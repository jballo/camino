"""Postgres integration tests for atomic claim and stale-lease recovery.

These need a real Postgres (``FOR UPDATE SKIP LOCKED`` semantics can't be
mocked). By default a uniquely named ``camino_worker_test_*`` scratch database
is created on the same server as ``DATABASE_URL`` and dropped afterwards; if
Postgres is unreachable the module skips. Set ``TEST_DATABASE_URL`` to use an
existing database instead (it will have ``jobs`` truncated); as a safety
net the fixture fails fast if it resolves to the same database as
``DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.main import app
from app.models.github_connection import GithubConnections
from app.models.tour import TourArtifact, TourStep
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import JOURNEY_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.worker import claim_next_job, recover_stale_jobs, run_job

SCRATCH_DB_PREFIX = "camino_worker_test"

WORKER_A = "host-a:1:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WORKER_B = "host-b:2:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture(scope="session")
def pg_engine():
    override = os.environ.get("TEST_DATABASE_URL")
    admin_engine = None
    scratch_db_name = None
    if override:
        url = make_url(override)
        app_url = make_url(settings.database_url)
        # Database names identify databases within a Postgres cluster. Rejecting
        # the application name unconditionally is deliberately conservative and
        # also covers host aliases such as localhost vs 127.0.0.1.
        if url.database == app_url.database:
            pytest.fail(
                "TEST_DATABASE_URL points at the application database "
                f"({app_url.database!r}); these tests TRUNCATE jobs. "
                "Use a dedicated test database or unset TEST_DATABASE_URL "
                "to auto-provision a scratch one."
            )
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
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_by TEXT"))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
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
        session.execute(text("TRUNCATE jobs RESTART IDENTITY CASCADE"))
        session.commit()
    return pg_engine


def _insert_job(session: Session, **overrides) -> Job:
    now = dt.datetime.now(dt.timezone.utc)
    job = Job(
        userId=overrides.get("userId", "user_1"),
        installation_id=overrides.get("installation_id", 1),
        repo_name=overrides.get("repo_name", "org/repo"),
        topic=overrides.get("topic", "topic"),
        job_type=overrides.get("job_type", JobType.TOUR),
        dedupe_key=overrides.get("dedupe_key"),
        status=overrides.get("status", JobStatus.PENDING),
        claimed_at=overrides.get("claimed_at"),
        claimed_by=overrides.get("claimed_by"),
        attempts=overrides.get("attempts", 0),
        error=overrides.get("error"),
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
                encryptedAccessToken="tok",
                encryptedRefreshToken="rtok",
                tokenExpiresAt=now + dt.timedelta(hours=1),
                refreshTokenExpiresAt=now + dt.timedelta(days=30),
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
