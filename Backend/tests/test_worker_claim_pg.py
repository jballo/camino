"""Postgres integration tests for atomic claim and stale-lease recovery.

These need a real Postgres (``FOR UPDATE SKIP LOCKED`` semantics can't be
mocked). By default a uniquely named ``camino_worker_test_*`` scratch database
is created on the same server as ``DATABASE_URL`` and dropped afterwards; if
Postgres is unreachable the module skips. Set ``TEST_DATABASE_URL`` to use an
existing database instead (it will have ``jobs`` truncated); as a safety
net the fixture fails fast if it resolves to the database that was configured
by ``DATABASE_URL`` before the test environment was sanitized.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app import main as app_main
from app.config import settings
from app.db import get_session
from app.main import app
from app.models.github_connection import GithubConnections
from app.models.code import RepoIndexState
from app.models.tour import TourArtifact, TourStep
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import JOURNEY_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.installation_state import (
    SUSPENSION_ERROR,
    set_installation_active,
)
from app.services.repo_access import RepoAccess
from app.services.jobs import cancel_job
from app.services.repository_ingestion import (
    IngestionCancelledError,
    SponsorInstallationInvalidError,
)
from app.services.shared_ingests import (
    MISSING_SHARED_INGEST_ERROR,
    enqueue_shared_ingest,
    release_user_jobs,
)
from app.worker import (
    _ensure_ingestion_owned,
    claim_next_job,
    park_job,
    recover_stale_jobs,
    release_shared_ingest,
    run_job,
)

SCRATCH_DB_PREFIX = "camino_worker_test"

WORKER_A = "host-a:1:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WORKER_B = "host-b:2:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_invalid_application_database_url_does_not_block_test_collection():
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "not a database URL"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/test_db_schema.py",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _test_database_url(
    override: str,
    application_database_name: str | None,
):
    url = make_url(override)
    if application_database_name is None:
        raise ValueError(
            "Cannot safely use TEST_DATABASE_URL because the original "
            "application database name is unavailable. Set DATABASE_URL to a "
            "parseable, non-target sentinel or use scripts/test_all.sh."
        )
    # Database names identify databases within a Postgres cluster. Rejecting
    # the application name unconditionally is deliberately conservative and
    # also covers host aliases such as localhost vs 127.0.0.1.
    if (
        application_database_name is not None
        and url.database == application_database_name
    ):
        raise ValueError(
            "TEST_DATABASE_URL points at the application database "
            f"({application_database_name!r}); these tests TRUNCATE jobs. "
            "Use a dedicated test database or unset TEST_DATABASE_URL "
            "to auto-provision a scratch one."
        )
    return url


def test_test_database_url_rejects_unknown_application_database():
    with pytest.raises(
        ValueError,
        match="original application database name is unavailable",
    ):
        _test_database_url("postgresql://localhost/testdb", None)


def test_test_database_url_rejects_pre_sanitization_application_database():
    with pytest.raises(
        ValueError,
        match="TEST_DATABASE_URL points at the application database",
    ):
        _test_database_url(
            "postgresql://localhost/onboarding_agent",
            "onboarding_agent",
        )


@pytest.fixture(scope="session")
def pg_engine(application_database_name):
    override = os.environ.get("TEST_DATABASE_URL")
    admin_engine = None
    scratch_db_name = None
    if override:
        try:
            url = _test_database_url(override, application_database_name)
        except ValueError as exc:
            pytest.fail(str(exc))
    else:
        # Auto-provision a uniquely named scratch DB on the same server as
        # DATABASE_URL. Never drop a pre-existing database: if the extremely
        # unlikely name collision occurs, CREATE DATABASE fails safely.
        scratch_db_name = f"{SCRATCH_DB_PREFIX}_{uuid.uuid4().hex}"
        # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
        base = make_url(settings.database_url)
        admin_engine = create_engine(
            base.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        try:
            with admin_engine.connect() as conn:
                conn.execute(text(f"CREATE DATABASE {scratch_db_name}"))
        except OperationalError as e:
            admin_engine.dispose()
            pytest.skip(f"Postgres not reachable ({e}); skipping claim integration tests")
        # Keep the URL object: str() would mask the password as "***".
        url = base.set(database=scratch_db_name)

    engine = create_engine(url)
    try:
        Job.__table__.create(engine, checkfirst=True)
    except OperationalError as e:
        engine.dispose()
        pytest.skip(f"Postgres not reachable ({e}); skipping claim integration tests")
    GithubConnections.__table__.create(engine, checkfirst=True)
    RepoIndexState.__table__.create(engine, checkfirst=True)
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_by TEXT"))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS blocked_by_job_id INTEGER"
        ))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS refresh_cycles INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_jobs_pending '
            "ON jobs (\"createdAt\") WHERE status = 'pending'"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_dedupe "
            "ON jobs (dedupe_key) WHERE status IN ('pending', 'running') "
            "AND dedupe_key IS NOT NULL"
        ))
        conn.commit()

    yield engine

    engine.dispose()
    if admin_engine is not None and scratch_db_name is not None:
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch_db_name}"))
        admin_engine.dispose()


@pytest.fixture
def pg_engine_clean(pg_engine):
    with Session(pg_engine) as session:
        session.execute(
            text(
                "TRUNCATE jobs, repo_index_state, githubconnections "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()
    return pg_engine


def _insert_job(session: Session, **overrides) -> Job:
    now = dt.datetime.now(dt.timezone.utc)
    user_id = overrides.get("userId", "user_1")
    installation_id = overrides.get("installation_id", 1)
    if overrides.get("with_connection", user_id is not None):
        connection = session.exec(
            select(GithubConnections).where(
                GithubConnections.userId == user_id,
                GithubConnections.installationId == installation_id,
            )
        ).one_or_none()
        if connection is None:
            session.add(
                GithubConnections(
                    userId=user_id,
                    githubUsername=f"github-{user_id}",
                    githubUserId=uuid.uuid4().int % 2_000_000_000,
                    installationId=installation_id,
                )
            )
            session.commit()
    job = Job(
        userId=user_id,
        installation_id=installation_id,
        repo_name=overrides.get("repo_name", "org/repo"),
        ref=overrides.get("ref", "main"),
        topic=overrides.get("topic", "topic"),
        job_type=overrides.get("job_type", JobType.TOUR),
        dedupe_key=overrides.get("dedupe_key"),
        status=overrides.get("status", JobStatus.PENDING),
        claimed_at=overrides.get("claimed_at"),
        claimed_by=overrides.get("claimed_by"),
        attempts=overrides.get("attempts", 0),
        error=overrides.get("error"),
        blocked_by_job_id=overrides.get("blocked_by_job_id"),
        refresh_cycles=overrides.get("refresh_cycles", 0),
        createdAt=overrides.get("createdAt", now),
        updatedAt=overrides.get("updatedAt", now),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    session.expunge(job)
    return job


def _reload(engine, job_id: int) -> Job:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


SHARED_KEY = "repository_ingest:org/repo:main"


def _insert_shared_ingest(session: Session, **overrides) -> Job:
    """The ownerless ingest for org/repo@main that waiting jobs block on."""
    return _insert_job(
        session,
        userId=None,
        installation_id=None,
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=SHARED_KEY,
        **overrides,
    )


def _insert_waiting_row(session: Session, shared: Job, **overrides) -> Job:
    user_id = overrides.pop("userId", "waiter")
    return _insert_job(
        session,
        userId=user_id,
        installation_id=overrides.pop("installation_id", 202),
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=f"{SHARED_KEY}:user:{user_id}",
        blocked_by_job_id=shared.id,
        **overrides,
    )


def _add_connection(
    session: Session, user_id: str, installation_id: int
) -> None:
    session.add(
        GithubConnections(
            userId=user_id,
            githubUsername=f"github-{user_id}",
            githubUserId=uuid.uuid4().int % 2_000_000_000,
            installationId=installation_id,
        )
    )
    session.commit()


def _deactivate_user(session: Session, user_id: str) -> None:
    session.execute(
        text('UPDATE githubconnections SET active = false WHERE "userId" = :user_id'),
        {"user_id": user_id},
    )
    session.commit()


def _fake_ingest(
    result: dict,
    calls: list[int],
    *,
    before_guard=None,
    rejected: frozenset[int] = frozenset(),
):
    """Stand-in for ``ingest_repository`` that saves once through the guard."""

    async def fake_ingest(
        session,
        *,
        installation_id,
        ensure_owned,
        finalize_publication,
        **_kwargs,
    ):
        calls.append(installation_id)
        if installation_id in rejected:
            raise SponsorInstallationInvalidError(
                f"installation {installation_id} is gone"
            )
        if before_guard is not None:
            before_guard(installation_id)
        ensure_owned(session)
        finalize_publication(session, result)
        session.commit()
        return result

    return fake_ingest


async def _run_with_ingest(engine, job_id: int, fake_ingest) -> None:
    with (
        patch("app.worker.engine", engine),
        patch("app.worker.ingest_repository", side_effect=fake_ingest),
    ):
        await run_job(job_id, WORKER_A)


# ── C. Claim atomicity ──────────────────────────────────────────────

def test_claim_exactly_one_winner_from_two_sessions(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job_id = _insert_job(session).id

    barrier = threading.Barrier(2)
    results: list[int | None] = [None, None]

    def attempt(index: int, worker_id: str) -> None:
        barrier.wait()
        with Session(pg_engine_clean) as session:
            results[index] = claim_next_job(session, worker_id)

    t1 = threading.Thread(target=attempt, args=(0, WORKER_A))
    t2 = threading.Thread(target=attempt, args=(1, WORKER_B))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    claimed = [jid for jid in results if jid is not None]
    missed = [jid for jid in results if jid is None]
    assert claimed == [job_id]
    assert len(missed) == 1


def test_two_claim_loops_drain_without_duplicates(pg_engine_clean):
    n = 20
    with Session(pg_engine_clean) as session:
        expected = {_insert_job(session, topic=f"t{i}").id for i in range(n)}

    def drain(worker_id: str) -> set[int]:
        claimed: set[int] = set()
        while True:
            with Session(pg_engine_clean) as session:
                job_id = claim_next_job(session, worker_id)
            if job_id is None:
                return claimed
            claimed.add(job_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(drain, WORKER_A)
        future_b = pool.submit(drain, WORKER_B)
        set_a = future_a.result()
        set_b = future_b.result()

    assert set_a.isdisjoint(set_b)
    assert set_a | set_b == expected


def test_claim_oldest_created_at_first(pg_engine_clean):
    older = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        old_job = _insert_job(session, topic="old", createdAt=older, updatedAt=older)
        old_id = old_job.id
        _insert_job(session, topic="new", createdAt=newer, updatedAt=newer)

    with Session(pg_engine_clean) as session:
        claimed = claim_next_job(session, WORKER_A)

    assert claimed == old_id


def test_claim_sets_generating_lease_and_attempts(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job = _insert_job(session, attempts=0)
        job_id = job.id

    with Session(pg_engine_clean) as session:
        claimed = claim_next_job(session, WORKER_A)

    assert claimed == job_id
    row = _reload(pg_engine_clean, job_id)
    assert row.status == JobStatus.RUNNING
    assert row.claimed_at is not None
    assert row.claimed_by == WORKER_A
    assert row.attempts == 1


def test_claim_skips_non_pending_rows(pg_engine_clean):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        _insert_job(session, status=JobStatus.RUNNING, claimed_at=now, claimed_by="w")
        _insert_job(session, status=JobStatus.COMPLETE)
        _insert_job(session, status=JobStatus.FAILED, error="nope")

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None


def test_claim_skips_job_with_inactive_owner_connection(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job = _insert_job(session)
        session.execute(
            text(
                'UPDATE githubconnections SET active = false '
                'WHERE "userId" = :user_id'
            ),
            {"user_id": job.userId},
        )
        session.commit()

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    assert _reload(pg_engine_clean, job.id).status == JobStatus.PENDING


def test_claim_skips_job_with_missing_connection(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job = _insert_job(session, with_connection=False)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    assert _reload(pg_engine_clean, job.id).status == JobStatus.PENDING


def test_job_becomes_claimable_after_owner_reconnects(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job = _insert_job(session)
        session.execute(
            text(
                'UPDATE githubconnections SET active = false '
                'WHERE "userId" = :user_id'
            ),
            {"user_id": job.userId},
        )
        session.commit()
        assert claim_next_job(session, WORKER_A) is None
        session.execute(
            text(
                'UPDATE githubconnections SET active = true '
                'WHERE "userId" = :user_id'
            ),
            {"user_id": job.userId},
        )
        session.commit()
        assert claim_next_job(session, WORKER_A) == job.id


def test_inactive_owner_at_queue_head_does_not_block_others(pg_engine_clean):
    older = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        blocked = _insert_job(
            session,
            userId="inactive_user",
            createdAt=older,
            updatedAt=older,
        )
        session.execute(
            text(
                'UPDATE githubconnections SET active = false '
                'WHERE "userId" = :user_id'
            ),
            {"user_id": blocked.userId},
        )
        session.commit()
        claimable = _insert_job(
            session,
            userId="active_user",
            createdAt=newer,
            updatedAt=newer,
        )

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == claimable.id
    assert _reload(pg_engine_clean, blocked.id).status == JobStatus.PENDING


def test_claim_skips_job_while_dependency_is_active(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        dependency = _insert_shared_ingest(session)
        blocked = _insert_job(session, blocked_by_job_id=dependency.id)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == dependency.id
    assert _reload(pg_engine_clean, blocked.id).status == JobStatus.PENDING

    barrier = threading.Barrier(2)
    results: list[int | None] = [blocked.id, blocked.id]

    def attempt(index: int, worker_id: str) -> None:
        barrier.wait()
        with Session(pg_engine_clean) as session:
            results[index] = claim_next_job(session, worker_id)

    first = threading.Thread(target=attempt, args=(0, WORKER_A))
    second = threading.Thread(target=attempt, args=(1, WORKER_B))
    first.start()
    second.start()
    first.join()
    second.join()
    assert results == [None, None]


@pytest.mark.parametrize("dependency_status", [JobStatus.COMPLETE])
def test_claim_unblocks_after_successful_dependency(pg_engine_clean, dependency_status):
    with Session(pg_engine_clean) as session:
        dependency = _insert_job(session, status=dependency_status)
        blocked = _insert_job(session, blocked_by_job_id=dependency.id)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == blocked.id


@pytest.mark.parametrize("dependency_status", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_claim_fails_job_after_unsuccessful_dependency(pg_engine_clean, dependency_status):
    with Session(pg_engine_clean) as session:
        dependency = _insert_job(
            session, status=dependency_status, error="clone failed"
        )
        blocked = _insert_job(session, blocked_by_job_id=dependency.id)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    row = _reload(pg_engine_clean, blocked.id)
    assert row.status == JobStatus.FAILED
    assert row.error == "ingest failed: clone failed"


def test_missing_dependency_fails_open(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        blocked = _insert_job(session, blocked_by_job_id=999999)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == blocked.id


# ── Shared ingest eligibility ───────────────────────────────────────

def test_shared_ingest_is_claimable_with_one_eligible_waiting_row(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session)
        waiting = _insert_waiting_row(session, shared)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared.id
        assert claim_next_job(session, WORKER_B) is None
    row = _reload(pg_engine_clean, shared.id)
    assert row.status == JobStatus.RUNNING
    assert row.userId is None
    assert row.installation_id is None
    assert _reload(pg_engine_clean, waiting.id).status == JobStatus.PENDING


@pytest.mark.parametrize(
    "waiter", ["none", "inactive", "mismatched_installation", "cancelled"]
)
def test_shared_ingest_is_not_claimable_without_eligible_waiting_job(
    pg_engine_clean, waiter
):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session)
        if waiter == "inactive":
            _insert_waiting_row(session, shared)
            _deactivate_user(session, "waiter")
        elif waiter == "mismatched_installation":
            # The user is connected, but not through the job's installation.
            _insert_waiting_row(
                session, shared, installation_id=202, with_connection=False
            )
            _add_connection(session, "waiter", 303)
        elif waiter == "cancelled":
            _insert_waiting_row(session, shared, status=JobStatus.CANCELLED)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    row = _reload(pg_engine_clean, shared.id)
    assert row.status == JobStatus.PENDING
    assert row.attempts == 0


def test_idle_shared_ingest_at_queue_head_does_not_block_others(pg_engine_clean):
    older = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        idle = _insert_shared_ingest(session, createdAt=older, updatedAt=older)
        claimable = _insert_job(session, createdAt=newer, updatedAt=newer)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == claimable.id
        assert claim_next_job(session, WORKER_A) is None
    assert _reload(pg_engine_clean, idle.id).status == JobStatus.PENDING


def test_brief_alone_makes_shared_ingest_claimable(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session)
        brief = _insert_job(
            session,
            userId="briefer",
            installation_id=303,
            job_type=JobType.ISSUE_BRIEF,
            blocked_by_job_id=shared.id,
        )

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared.id
    assert _reload(pg_engine_clean, brief.id).status == JobStatus.PENDING


def test_suspended_users_brief_is_cancelled_while_teammate_keeps_shared_ingest(
    pg_engine_clean,
):
    older = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session, createdAt=older, updatedAt=older)
        own_brief = _insert_job(
            session,
            userId="suspended",
            installation_id=101,
            job_type=JobType.ISSUE_BRIEF,
            blocked_by_job_id=shared.id,
            createdAt=older,
            updatedAt=older,
        )
        teammate_brief = _insert_job(
            session,
            userId="teammate",
            installation_id=202,
            job_type=JobType.ISSUE_BRIEF,
            blocked_by_job_id=shared.id,
            createdAt=newer,
            updatedAt=newer,
        )

    with Session(pg_engine_clean) as session:
        set_installation_active(session, 101, active=False)

    own = _reload(pg_engine_clean, own_brief.id)
    assert own.status == JobStatus.CANCELLED
    assert own.error == SUSPENSION_ERROR
    untouched = _reload(pg_engine_clean, shared.id)
    assert untouched.status == JobStatus.PENDING
    assert untouched.updatedAt == older

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared.id
        session.execute(
            text("UPDATE jobs SET status = 'complete' WHERE id = :id"),
            {"id": shared.id},
        )
        session.commit()
        assert claim_next_job(session, WORKER_A) == teammate_brief.id
        assert claim_next_job(session, WORKER_A) is None

    with Session(pg_engine_clean) as session:
        set_installation_active(session, 101, active=True)
        assert claim_next_job(session, WORKER_A) is None
    assert _reload(pg_engine_clean, own_brief.id).status == JobStatus.CANCELLED


# ── Waiting rows settle at claim ────────────────────────────────────

def test_waiting_row_completes_from_shared_ingest_without_running(pg_engine_clean):
    artifact = {"chunks_inserted": 12, "embeddings_created": 12}
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session, status=JobStatus.COMPLETE)
        session.execute(
            text("UPDATE jobs SET artifact = CAST(:artifact AS JSONB) WHERE id = :id"),
            {"artifact": json.dumps(artifact), "id": shared.id},
        )
        session.commit()
        waiting = _insert_waiting_row(session, shared, attempts=1)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None

    row = _reload(pg_engine_clean, waiting.id)
    assert row.status == JobStatus.COMPLETE
    assert row.artifact == artifact
    assert row.attempts == 1
    assert row.claimed_at is None
    assert row.claimed_by is None


def test_waiting_row_fails_when_shared_ingest_failed(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(
            session,
            status=JobStatus.FAILED,
            error="ref not found",
        )
        waiting = _insert_waiting_row(session, shared)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None

    row = _reload(pg_engine_clean, waiting.id)
    assert row.status == JobStatus.FAILED
    assert row.error == "ingest failed: ref not found"


def test_legacy_owned_ingest_without_dependency_fails_instead_of_running(
    pg_engine_clean,
):
    with Session(pg_engine_clean) as session:
        legacy = _insert_job(
            session,
            job_type=JobType.REPOSITORY_INGEST,
            attempts=1,
        )

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None

    row = _reload(pg_engine_clean, legacy.id)
    assert row.status == JobStatus.FAILED
    assert row.error == MISSING_SHARED_INGEST_ERROR
    assert "request it again" in row.error
    assert row.attempts == 1


async def _lifespan_conversion_sql() -> str:
    """The startup statement that converts owned shared ingests."""
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection
    with (
        patch.object(app_main, "engine", mock_engine),
        patch.object(app_main.SQLModel.metadata, "create_all"),
        patch.object(app_main, "verify_embedding_schema"),
    ):
        async with app_main.lifespan(app_main.app):
            pass
    (statement,) = [
        str(call.args[0])
        for call in connection.execute.call_args_list
        if "WITH owned AS" in str(call.args[0])
    ]
    return statement


def _jobs_snapshot(engine) -> list[tuple]:
    with Session(engine) as session:
        return [
            (
                job.id,
                job.userId,
                job.installation_id,
                job.status,
                job.dedupe_key,
                job.blocked_by_job_id,
                job.claimed_by,
                job.attempts,
            )
            for job in session.exec(select(Job).order_by(Job.id)).all()
        ]


async def test_lifespan_converts_owned_shared_ingest_once(pg_engine_clean):
    conversion = text(await _lifespan_conversion_sql())
    now = dt.datetime.now(dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        owned = _insert_job(
            session,
            userId="owner",
            installation_id=101,
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key=SHARED_KEY,
            status=JobStatus.RUNNING,
            claimed_at=now,
            claimed_by=WORKER_A,
            attempts=1,
        )
        # Not converted: no global dedupe key, already finished, or already
        # a waiting row.
        legacy = _insert_job(
            session,
            userId="owner",
            installation_id=101,
            repo_name="org/legacy",
            job_type=JobType.REPOSITORY_INGEST,
        )
        finished = _insert_job(
            session,
            userId="owner",
            installation_id=101,
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key=SHARED_KEY,
            status=JobStatus.COMPLETE,
        )
        existing_waiter = _insert_waiting_row(session, owned)

    with Session(pg_engine_clean) as session:
        session.execute(conversion)
        session.commit()

    converted = _reload(pg_engine_clean, owned.id)
    assert converted.userId is None
    assert converted.installation_id is None
    assert converted.status == JobStatus.RUNNING
    assert converted.claimed_by == WORKER_A
    assert converted.attempts == 1
    for unchanged in (legacy, finished, existing_waiter):
        row = _reload(pg_engine_clean, unchanged.id)
        assert row.userId == unchanged.userId
        assert row.installation_id == unchanged.installation_id
        assert row.status == unchanged.status
    with Session(pg_engine_clean) as session:
        waiting_rows = session.exec(
            select(Job).where(
                Job.blocked_by_job_id == owned.id,
                Job.userId == "owner",
            )
        ).all()
    assert len(waiting_rows) == 1
    (owner_row,) = waiting_rows
    assert owner_row.installation_id == 101
    assert owner_row.job_type == JobType.REPOSITORY_INGEST
    assert owner_row.dedupe_key == f"{SHARED_KEY}:user:owner"
    assert owner_row.status == JobStatus.PENDING

    # The run in flight keeps passing its guard under the owner's installation.
    with Session(pg_engine_clean) as session:
        _ensure_ingestion_owned(
            session,
            job_id=owned.id,
            worker_id=WORKER_A,
            installation_id=101,
            lease_lost=threading.Event(),
        )
        session.rollback()

    snapshot = _jobs_snapshot(pg_engine_clean)
    with Session(pg_engine_clean) as session:
        session.execute(conversion)
        session.commit()
    assert _jobs_snapshot(pg_engine_clean) == snapshot


ORPHANED_ACTIVE_JOBS_SQL = text("""
SELECT j.id FROM jobs AS j
WHERE j.status IN ('pending', 'running')
  AND j."userId" IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM githubconnections AS c
      WHERE c."userId" = j."userId"
        AND c."installationId" = j.installation_id
        AND c.active IS TRUE
  )
""")


def test_concurrent_removals_of_every_waiting_user_leave_shared_ingest_idle(
    pg_engine_clean,
):
    older = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session, createdAt=older, updatedAt=older)
        first_row = _insert_waiting_row(
            session, shared, userId="first", installation_id=101
        )
        second_row = _insert_waiting_row(
            session, shared, userId="second", installation_id=202
        )

    first = Session(pg_engine_clean)
    try:
        # Hold the first removal uncommitted while the second runs. Neither
        # touches the shared ingest or the other user's rows, so the second
        # never waits on the first.
        first.execute(
            update(GithubConnections)
            .where(GithubConnections.installationId == 101)
            .values(active=False)
        )
        release_user_jobs(
            first, {"first"}, error=SUSPENSION_ERROR, dispose="cancel"
        )
        errors: list[BaseException] = []

        def remove_second() -> None:
            try:
                with Session(pg_engine_clean) as session:
                    session.execute(text("SET LOCAL lock_timeout = '2s'"))
                    set_installation_active(session, 202, active=False)
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        thread = threading.Thread(target=remove_second)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert errors == []
        first.commit()
    finally:
        first.close()

    for waiting in (first_row, second_row):
        row = _reload(pg_engine_clean, waiting.id)
        assert row.status == JobStatus.CANCELLED
        assert row.error == SUSPENSION_ERROR
    idle = _reload(pg_engine_clean, shared.id)
    assert idle.status == JobStatus.PENDING
    assert idle.userId is None
    assert idle.installation_id is None
    assert idle.attempts == 0
    assert idle.updatedAt == older
    with Session(pg_engine_clean) as session:
        assert session.execute(ORPHANED_ACTIVE_JOBS_SQL).all() == []
        assert claim_next_job(session, WORKER_A) is None


# ── Running a shared ingest ─────────────────────────────────────────

def _connect_users(engine, *users: tuple[str, int]) -> None:
    with Session(engine) as session:
        for user_id, installation_id in users:
            _add_connection(session, user_id, installation_id)


def _request_ingest(engine, user_id: str, installation_id: int) -> tuple[int, int]:
    """Return ``(waiting_row_id, shared_id)`` for a user's ingest request."""
    with Session(engine) as session:
        waiting, shared, _ = enqueue_shared_ingest(
            session,
            user_id=user_id,
            installation_id=installation_id,
            repo_name="org/repo",
            ref="main",
        )
        return waiting.id, shared.id


def test_release_returns_shared_ingest_to_pending_with_attempt_refunded(
    pg_engine_clean,
):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session)
        _insert_waiting_row(session, shared)
        assert claim_next_job(session, WORKER_A) == shared.id
        assert release_shared_ingest(session, shared.id, WORKER_B) is False
        assert _reload(pg_engine_clean, shared.id).status == JobStatus.RUNNING
        assert release_shared_ingest(session, shared.id, WORKER_A) is True

    row = _reload(pg_engine_clean, shared.id)
    assert row.status == JobStatus.PENDING
    assert row.attempts == 0
    assert row.claimed_at is None
    assert row.claimed_by is None
    assert row.error is None
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared.id


async def test_run_releases_shared_ingest_when_nobody_is_waiting(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session)
        waiting = _insert_waiting_row(session, shared)
        assert claim_next_job(session, WORKER_A) == shared.id
        assert cancel_job(session, waiting.id)

    calls: list[int] = []
    await _run_with_ingest(pg_engine_clean, shared.id, _fake_ingest({}, calls))

    assert calls == []
    row = _reload(pg_engine_clean, shared.id)
    assert row.status == JobStatus.PENDING
    assert row.attempts == 0
    assert row.claimed_by is None
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None


async def test_shared_ingest_restarts_for_second_waiter_after_sponsor_suspension(
    pg_engine_clean,
):
    result = {"chunks_inserted": 3, "embeddings_created": 3}
    _connect_users(pg_engine_clean, ("first", 101), ("second", 202))
    first_id, shared_id = _request_ingest(pg_engine_clean, "first", 101)
    second_id, joined_id = _request_ingest(pg_engine_clean, "second", 202)
    assert joined_id == shared_id

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared_id

    def suspend_sponsor(installation_id: int) -> None:
        if installation_id == 101:
            with Session(pg_engine_clean) as session:
                set_installation_active(session, 101, active=False)

    calls: list[int] = []
    fake = _fake_ingest(result, calls, before_guard=suspend_sponsor)
    await _run_with_ingest(pg_engine_clean, shared_id, fake)

    assert calls == [101]
    released = _reload(pg_engine_clean, shared_id)
    assert released.status == JobStatus.PENDING
    assert released.attempts == 0
    assert released.claimed_by is None
    first = _reload(pg_engine_clean, first_id)
    assert first.status == JobStatus.CANCELLED
    assert first.error == SUSPENSION_ERROR
    assert _reload(pg_engine_clean, second_id).status == JobStatus.PENDING

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared_id
    await _run_with_ingest(pg_engine_clean, shared_id, fake)

    assert calls == [101, 202]
    completed = _reload(pg_engine_clean, shared_id)
    assert completed.status == JobStatus.COMPLETE
    assert completed.attempts == 1
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    second = _reload(pg_engine_clean, second_id)
    assert second.status == JobStatus.COMPLETE
    assert second.artifact == result
    assert second.error is None


@pytest.mark.parametrize(
    ("joiner_installation", "expected_calls"),
    [
        # A teammate on the sponsor's installation keeps the run going.
        (101, [101]),
        # Anyone else releases it; it restarts under their installation.
        (202, [101, 202]),
    ],
)
async def test_join_racing_last_cancel_never_fails_new_requester(
    pg_engine_clean, joiner_installation, expected_calls
):
    result = {"chunks_inserted": 5, "embeddings_created": 5}
    _connect_users(pg_engine_clean, ("first", 101), ("joiner", joiner_installation))
    first_id, shared_id = _request_ingest(pg_engine_clean, "first", 101)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared_id

    joined: list[tuple[int, int]] = []
    errors: list[BaseException] = []

    def join_and_cancel(installation_id: int) -> None:
        if installation_id != 101 or joined:
            return
        barrier = threading.Barrier(2)

        def cancel() -> None:
            try:
                barrier.wait()
                with Session(pg_engine_clean) as session:
                    assert cancel_job(session, first_id)
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        def join() -> None:
            try:
                barrier.wait()
                joined.append(
                    _request_ingest(pg_engine_clean, "joiner", joiner_installation)
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        threads = [threading.Thread(target=cancel), threading.Thread(target=join)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    calls: list[int] = []
    fake = _fake_ingest(result, calls, before_guard=join_and_cancel)
    await _run_with_ingest(pg_engine_clean, shared_id, fake)
    assert errors == []
    (joiner_id, joined_shared_id), = joined
    assert joined_shared_id == shared_id
    assert _reload(pg_engine_clean, joiner_id).status == JobStatus.PENDING

    if _reload(pg_engine_clean, shared_id).status == JobStatus.PENDING:
        with Session(pg_engine_clean) as session:
            assert claim_next_job(session, WORKER_A) == shared_id
        await _run_with_ingest(pg_engine_clean, shared_id, fake)

    assert calls == expected_calls
    assert _reload(pg_engine_clean, shared_id).status == JobStatus.COMPLETE
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    joiner = _reload(pg_engine_clean, joiner_id)
    assert joiner.status == JobStatus.COMPLETE
    assert joiner.artifact == result
    assert _reload(pg_engine_clean, first_id).status == JobStatus.CANCELLED


async def test_dead_sponsor_installation_falls_back_to_next_waiting_user(
    pg_engine_clean,
):
    result = {"chunks_inserted": 2, "embeddings_created": 2}
    _connect_users(pg_engine_clean, ("first", 101), ("second", 202))
    first_id, shared_id = _request_ingest(pg_engine_clean, "first", 101)
    second_id, _ = _request_ingest(pg_engine_clean, "second", 202)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared_id

    calls: list[int] = []
    fake = _fake_ingest(result, calls, rejected=frozenset({101}))
    await _run_with_ingest(pg_engine_clean, shared_id, fake)

    assert calls == [101, 202]
    completed = _reload(pg_engine_clean, shared_id)
    assert completed.status == JobStatus.COMPLETE
    assert completed.attempts == 1
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    for waiting_id in (first_id, second_id):
        row = _reload(pg_engine_clean, waiting_id)
        assert row.status == JobStatus.COMPLETE
        assert row.artifact == result


async def test_sponsor_joining_after_others_are_rejected_is_tried(
    pg_engine_clean,
):
    result = {"chunks_inserted": 3, "embeddings_created": 3}
    _connect_users(pg_engine_clean, ("first", 101), ("second", 202))
    first_id, shared_id = _request_ingest(pg_engine_clean, "first", 101)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared_id

    calls: list[int] = []
    joined: list[tuple[int, int]] = []
    sponsor_ingest = _fake_ingest(result, calls, rejected=frozenset({101}))

    async def fake_ingest(session, *, installation_id, **kwargs):
        # "second" joins after the sponsor list was read and while the only
        # listed installation is being rejected.
        if installation_id == 101:
            joined.append(_request_ingest(pg_engine_clean, "second", 202))
        return await sponsor_ingest(
            session, installation_id=installation_id, **kwargs
        )

    await _run_with_ingest(pg_engine_clean, shared_id, fake_ingest)

    (second_id, joined_shared_id), = joined
    assert joined_shared_id == shared_id
    assert calls == [101, 202]
    assert _reload(pg_engine_clean, shared_id).status == JobStatus.COMPLETE
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    for waiting_id in (first_id, second_id):
        row = _reload(pg_engine_clean, waiting_id)
        assert row.status == JobStatus.COMPLETE
        assert row.artifact == result


async def test_shared_ingest_is_released_when_every_sponsor_is_rejected(
    pg_engine_clean,
):
    result = {"chunks_inserted": 4, "embeddings_created": 4}
    _connect_users(
        pg_engine_clean, ("first", 101), ("second", 202), ("third", 303)
    )
    first_id, shared_id = _request_ingest(pg_engine_clean, "first", 101)
    second_id, _ = _request_ingest(pg_engine_clean, "second", 202)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared_id

    calls: list[int] = []
    fake = _fake_ingest(result, calls, rejected=frozenset({101, 202}))
    await _run_with_ingest(pg_engine_clean, shared_id, fake)

    assert calls == [101, 202]
    released = _reload(pg_engine_clean, shared_id)
    assert released.status == JobStatus.PENDING
    assert released.attempts == 0
    for waiting_id, installation_id in ((first_id, 101), (second_id, 202)):
        row = _reload(pg_engine_clean, waiting_id)
        assert row.status == JobStatus.FAILED
        assert row.error == f"installation {installation_id} is gone"
    # Idle until someone eligible waits, not spinning on the dead waiters.
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None

    third_id, third_shared_id = _request_ingest(pg_engine_clean, "third", 303)
    assert third_shared_id == shared_id
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared_id
    await _run_with_ingest(pg_engine_clean, shared_id, fake)

    assert calls == [101, 202, 303]
    assert _reload(pg_engine_clean, shared_id).status == JobStatus.COMPLETE
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    third = _reload(pg_engine_clean, third_id)
    assert third.status == JobStatus.COMPLETE
    assert third.artifact == result


def test_parking_preserves_retry_budget_and_created_at(pg_engine_clean):
    created = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        dependency = _insert_shared_ingest(session)
        job = _insert_job(
            session,
            status=JobStatus.RUNNING,
            claimed_at=dt.datetime.now(dt.timezone.utc),
            claimed_by=WORKER_A,
            attempts=2,
            createdAt=created,
            updatedAt=created,
        )
        assert park_job(
            session, job.id, WORKER_A, blocked_by_job_id=dependency.id
        )
    row = _reload(pg_engine_clean, job.id)
    assert row.status == JobStatus.PENDING
    assert row.attempts == 1
    assert row.refresh_cycles == 1
    assert row.blocked_by_job_id == dependency.id
    assert row.claimed_at is None
    assert row.claimed_by is None
    assert row.createdAt == created


# ── D. Stale recovery ───────────────────────────────────────────────

def test_stale_generating_job_is_requeued(pg_engine_clean):
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    with Session(pg_engine_clean) as session:
        job = _insert_job(
            session,
            status=JobStatus.RUNNING,
            claimed_at=stale,
            claimed_by=WORKER_A,
            attempts=1,
        )
        job_id = job.id

    with Session(pg_engine_clean) as session:
        recovered = recover_stale_jobs(
            session, lease_timeout_seconds=60, max_attempts=3
        )

    assert recovered == 1
    row = _reload(pg_engine_clean, job_id)
    assert row.status == JobStatus.PENDING
    assert row.claimed_at is None
    assert row.claimed_by is None
    assert row.attempts == 1


def test_legacy_generating_job_without_lease_is_requeued(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job = _insert_job(
            session,
            status=JobStatus.RUNNING,
            claimed_at=None,
            claimed_by=None,
            attempts=0,
        )
        job_id = job.id

    with Session(pg_engine_clean) as session:
        recovered = recover_stale_jobs(
            session, lease_timeout_seconds=60, max_attempts=3
        )

    assert recovered == 1
    row = _reload(pg_engine_clean, job_id)
    assert row.status == JobStatus.PENDING
    assert row.claimed_at is None
    assert row.claimed_by is None
    assert row.attempts == 0


def test_stale_job_at_max_attempts_is_failed(pg_engine_clean):
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    with Session(pg_engine_clean) as session:
        job = _insert_job(
            session,
            status=JobStatus.RUNNING,
            claimed_at=stale,
            claimed_by=WORKER_A,
            attempts=3,
        )
        job_id = job.id

    with Session(pg_engine_clean) as session:
        recover_stale_jobs(session, lease_timeout_seconds=60, max_attempts=3)

    row = _reload(pg_engine_clean, job_id)
    assert row.status == JobStatus.FAILED
    assert row.error == "Exceeded max attempts"
    assert row.claimed_at is None
    assert row.claimed_by is None


def test_fresh_generating_job_is_untouched(pg_engine_clean):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(pg_engine_clean) as session:
        job = _insert_job(
            session,
            status=JobStatus.RUNNING,
            claimed_at=now,
            claimed_by=WORKER_A,
            attempts=1,
        )
        job_id = job.id

    with Session(pg_engine_clean) as session:
        recovered = recover_stale_jobs(
            session, lease_timeout_seconds=1800, max_attempts=3
        )

    assert recovered == 0
    row = _reload(pg_engine_clean, job_id)
    assert row.status == JobStatus.RUNNING
    assert row.claimed_by == WORKER_A
    assert row.claimed_at is not None


def _running_shared_ingest(session: Session) -> Job:
    return _insert_shared_ingest(
        session,
        status=JobStatus.RUNNING,
        claimed_at=dt.datetime.now(dt.timezone.utc),
        claimed_by=WORKER_A,
        attempts=1,
    )


def _guard(engine, job_id: int, installation_id: int) -> None:
    with Session(engine) as session:
        _ensure_ingestion_owned(
            session,
            job_id=job_id,
            worker_id=WORKER_A,
            installation_id=installation_id,
            lease_lost=threading.Event(),
        )
        session.rollback()


def test_ingestion_guard_requires_current_claim_and_waiting_user(pg_engine_clean):
    installation_id = uuid.uuid4().int % 2_000_000_000
    with Session(pg_engine_clean) as session:
        job_id = _running_shared_ingest(session).id
        _insert_waiting_row(
            session,
            _reload(pg_engine_clean, job_id),
            installation_id=installation_id,
        )

    with Session(pg_engine_clean) as session:
        _ensure_ingestion_owned(
            session,
            job_id=job_id,
            worker_id=WORKER_A,
            installation_id=installation_id,
            lease_lost=threading.Event(),
        )

        with Session(pg_engine_clean) as deleting_session:
            deleting_session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(OperationalError):
                deleting_session.execute(
                    text(
                        'DELETE FROM githubconnections WHERE "installationId" = '
                        ":installation_id"
                    ),
                    {"installation_id": installation_id},
                )
                deleting_session.commit()
            deleting_session.rollback()

        session.rollback()

    # Nobody waiting on the ingest uses another installation.
    with pytest.raises(IngestionCancelledError, match="Nobody eligible"):
        _guard(pg_engine_clean, job_id, installation_id + 1)

    with Session(pg_engine_clean) as session:
        session.execute(
            text("UPDATE jobs SET claimed_by = :worker WHERE id = :job_id"),
            {"worker": WORKER_B, "job_id": job_id},
        )
        session.commit()

    with pytest.raises(IngestionCancelledError, match="no longer active"):
        _guard(pg_engine_clean, job_id, installation_id)

    with Session(pg_engine_clean) as session:
        session.execute(
            text("UPDATE jobs SET claimed_by = :worker WHERE id = :job_id"),
            {"worker": WORKER_A, "job_id": job_id},
        )
        session.execute(
            text(
                'DELETE FROM githubconnections WHERE "installationId" = '
                ":installation_id"
            ),
            {"installation_id": installation_id},
        )
        session.commit()

    with pytest.raises(IngestionCancelledError, match="Nobody eligible"):
        _guard(pg_engine_clean, job_id, installation_id)


def test_ingestion_guard_fails_when_only_waiting_user_cancels(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        shared = _running_shared_ingest(session)
        waiting = _insert_waiting_row(session, shared, installation_id=101)

    _guard(pg_engine_clean, shared.id, 101)

    with Session(pg_engine_clean) as session:
        assert cancel_job(session, waiting.id)

    with pytest.raises(IngestionCancelledError, match="Nobody eligible"):
        _guard(pg_engine_clean, shared.id, 101)


def test_ingestion_guard_passes_when_teammate_on_installation_is_waiting(
    pg_engine_clean,
):
    with Session(pg_engine_clean) as session:
        shared = _running_shared_ingest(session)
        _insert_waiting_row(session, shared, userId="sponsor", installation_id=101)
        _insert_waiting_row(session, shared, userId="teammate", installation_id=101)

    # The sponsor revokes: their connection and their rows go away.
    with Session(pg_engine_clean) as session:
        session.execute(
            text('DELETE FROM githubconnections WHERE "userId" = :user_id'),
            {"user_id": "sponsor"},
        )
        release_user_jobs(
            session, {"sponsor"}, error="revoked", dispose="cancel"
        )
        session.commit()

    _guard(pg_engine_clean, shared.id, 101)


def test_ingestion_guard_rejects_inactive_sponsor_when_teammate_is_not_waiting(
    pg_engine_clean,
):
    installation_id = uuid.uuid4().int % 2_000_000_000
    with Session(pg_engine_clean) as session:
        shared = _running_shared_ingest(session)
        _insert_waiting_row(
            session,
            shared,
            userId="inactive_sponsor",
            installation_id=installation_id,
        )
        _deactivate_user(session, "inactive_sponsor")
        # Active on the same installation, but not waiting on this ingest.
        _add_connection(session, "active_teammate", installation_id)

    with pytest.raises(IngestionCancelledError, match="Nobody eligible"):
        _guard(pg_engine_clean, shared.id, installation_id)


# ── E. Happy-path DB-queue handoff ──────────────────────────────────

def _artifact() -> TourArtifact:
    return TourArtifact(
        title="Auth tour",
        topic="authentication flow",
        repo_name="org/repo",
        steps=[
            TourStep(
                title="Validate JWT",
                explanation="Checks the token.",
                file_path="auth.py",
                start_line=1,
                end_line=10,
                snippet="def validate(): ...",
            )
        ],
    )


async def test_post_then_worker_then_get_completes(pg_engine_clean):
    now = dt.datetime.now(dt.timezone.utc)
    test_user = "test_worker_claim_pg"
    with Session(pg_engine_clean) as session:
        for row in session.exec(
            select(GithubConnections).where(GithubConnections.userId == test_user)
        ).all():
            session.delete(row)
        session.commit()
        session.add(
            GithubConnections(
                userId=test_user,
                githubUsername="octocat",
                githubUserId=1,
                installationId=12345,
            )
        )
        session.add(
            RepoIndexState(
                repo_name="org/repo",
                ref="main",
                visibility="public",
                active_generation="generation-1",
            )
        )
        session.commit()

    def _session():
        with Session(pg_engine_clean) as session:
            yield session

    app.dependency_overrides[get_authenticated_user_id] = lambda: test_user
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[JOURNEY_CREATE_RATE_LIMIT] = lambda: None
    artifact = _artifact()
    client = TestClient(app)

    access_probe = patch(
        "app.services.repo_access.resolve_repo_access",
        return_value=RepoAccess(installation_id=12345, visibility="public"),
    )
    access_probe.start()
    try:
        created = client.post(
            "/api/v1/journeys",
            json={"repoName": "org/repo", "topic": "authentication flow"},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        assert created.json()["status"] == JobStatus.PENDING

        duplicate = client.post(
            "/api/v1/journeys",
            json={"repoName": "org/repo", "topic": "authentication flow"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json() == {
            "id": job_id,
            "status": JobStatus.PENDING,
        }

        with Session(pg_engine_clean) as session:
            claimed = claim_next_job(session, WORKER_A)
        assert claimed == job_id

        with (
            patch("app.worker.engine", pg_engine_clean),
            patch(
                "app.worker.generate_tour",
                new_callable=AsyncMock,
                return_value=artifact,
            ),
        ):
            await run_job(job_id, WORKER_A)

        fetched = client.get(f"/api/v1/journeys/{job_id}")
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["status"] == JobStatus.COMPLETE
        assert body["artifact"] == artifact.model_dump()
        assert body["error"] is None
    finally:
        access_probe.stop()
        app.dependency_overrides.clear()


async def test_active_job_heartbeat_prevents_stale_recovery(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job_id = _insert_job(session).id
        assert claim_next_job(session, WORKER_A) == job_id

    artifact = _artifact()

    async def _generate_slowly(*_args, **_kwargs):
        await asyncio.sleep(0.15)
        with Session(pg_engine_clean) as recovery_session:
            recovered = recover_stale_jobs(
                recovery_session,
                lease_timeout_seconds=0.06,
                max_attempts=3,
            )
        assert recovered == 0
        return artifact

    with (
        patch("app.worker.engine", pg_engine_clean),
        patch("app.worker.settings.worker_lease_timeout", 0.06),
        patch("app.worker.generate_tour", side_effect=_generate_slowly),
    ):
        await run_job(job_id, WORKER_A)

    row = _reload(pg_engine_clean, job_id)
    assert row.status == JobStatus.COMPLETE
    assert row.artifact == artifact.model_dump()


async def test_old_worker_cannot_persist_after_job_is_reclaimed(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        job_id = _insert_job(session).id
        assert claim_next_job(session, WORKER_A) == job_id

    artifact = _artifact()

    async def _reclaim_during_generation(*_args, **_kwargs):
        stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
        with Session(pg_engine_clean) as session:
            session.execute(
                text(
                    "UPDATE jobs SET claimed_at = :stale "
                    "WHERE id = :job_id"
                ),
                {"stale": stale, "job_id": job_id},
            )
            session.commit()
            assert recover_stale_jobs(
                session,
                lease_timeout_seconds=60,
                max_attempts=3,
            ) == 1
            assert claim_next_job(session, WORKER_B) == job_id
        return artifact

    with (
        patch("app.worker.engine", pg_engine_clean),
        patch("app.worker.generate_tour", side_effect=_reclaim_during_generation),
    ):
        await run_job(job_id, WORKER_A)

    row = _reload(pg_engine_clean, job_id)
    assert row.status == JobStatus.RUNNING
    assert row.claimed_by == WORKER_B
    assert row.artifact is None
