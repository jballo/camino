"""Postgres integration tests for deleting an issue brief.

Deletion removes the row at any status, so these check what a real database
does around it: the worker that was running the brief, the active dedupe
index, and the shared ingest the brief may have been waiting on. Database
setup follows tests/test_worker_claim_pg.py.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import exc
from sqlmodel import Session, select

from app.api.briefs import delete_brief, get_brief
from app.brief import BriefNeedsRefreshError
from app.models.job import Job, JobStatus, JobType
from app.worker import _renew_job_lease, _update_owned_job, claim_next_job, run_job
from tests.test_worker_claim_pg import (  # noqa: F401  (fixtures)
    WORKER_A,
    _artifact,
    _insert_job,
    _insert_shared_ingest,
    _reload,
    pg_engine,
    pg_engine_clean,
)

OWNER = "brief_owner"
BRIEF_KEY = f"{JobType.ISSUE_BRIEF}:{OWNER}:org/repo:main:org/repo:44"


def _insert_brief(session: Session, **overrides) -> Job:
    return _insert_job(
        session,
        userId=overrides.pop("userId", OWNER),
        installation_id=overrides.pop("installation_id", 101),
        job_type=JobType.ISSUE_BRIEF,
        dedupe_key=overrides.pop("dedupe_key", BRIEF_KEY),
        **overrides,
    )


async def _delete(engine, job_id: int, user_id: str = OWNER) -> None:
    with Session(engine) as session:
        await delete_brief(job_id, session, user_id)


def _exists(engine, job_id: int) -> bool:
    with Session(engine) as session:
        return session.get(Job, job_id) is not None


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.COMPLETE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ],
)
async def test_brief_is_deleted_at_every_status(pg_engine_clean, status):
    with Session(pg_engine_clean) as session:
        brief = _insert_brief(session, status=status)

    await _delete(pg_engine_clean, brief.id)

    assert not _exists(pg_engine_clean, brief.id)
    with Session(pg_engine_clean) as session:
        with pytest.raises(HTTPException) as missing:
            await get_brief(brief.id, session, OWNER)
    assert missing.value.status_code == 404


async def test_another_users_brief_is_not_deleted(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        brief = _insert_brief(session)

    with pytest.raises(HTTPException) as forbidden:
        await _delete(pg_engine_clean, brief.id, user_id="someone_else")

    assert forbidden.value.status_code == 403
    assert _exists(pg_engine_clean, brief.id)


async def test_failed_commit_keeps_the_brief(pg_engine_clean):
    with Session(pg_engine_clean) as session:
        brief = _insert_brief(session)

    with Session(pg_engine_clean) as session:
        with (
            patch.object(
                session,
                "commit",
                side_effect=exc.OperationalError("DELETE", {}, Exception("down")),
            ),
            pytest.raises(HTTPException) as failed,
        ):
            await delete_brief(brief.id, session, OWNER)

    assert failed.value.status_code == 500
    assert _exists(pg_engine_clean, brief.id)


async def test_old_worker_cannot_touch_a_recreated_brief(pg_engine_clean):
    """Delete a running brief, re-request the same issue, then let the old
    worker try to finish: its writes must miss the replacement."""
    with Session(pg_engine_clean) as session:
        running = _insert_brief(session)
        assert claim_next_job(session, WORKER_A) == running.id

    await _delete(pg_engine_clean, running.id)

    # The active dedupe index would reject this if the old row still held
    # the key.
    with Session(pg_engine_clean) as session:
        replacement = _insert_brief(session)

    with patch("app.worker.engine", pg_engine_clean):
        assert _renew_job_lease(running.id, WORKER_A) is False
    with Session(pg_engine_clean) as session:
        assert (
            _update_owned_job(
                session,
                running.id,
                WORKER_A,
                status=JobStatus.COMPLETE,
                artifact={"summary": "stale"},
                claimed_at=None,
                claimed_by=None,
            )
            is False
        )

    row = _reload(pg_engine_clean, replacement.id)
    assert row.status == JobStatus.PENDING
    assert row.artifact is None
    assert row.claimed_by is None
    assert not _exists(pg_engine_clean, running.id)


async def test_worker_discards_outcome_when_row_is_deleted_mid_run(
    pg_engine_clean,
):
    """run_job finishes quietly when its row disappears during generation,
    and does not write the row back."""
    with Session(pg_engine_clean) as session:
        job_id = _insert_job(session).id
        assert claim_next_job(session, WORKER_A) == job_id

    async def _delete_during_generation(*_args, **_kwargs):
        with Session(pg_engine_clean) as session:
            session.delete(session.get(Job, job_id))
            session.commit()
        return _artifact()

    with (
        patch("app.worker.engine", pg_engine_clean),
        patch("app.worker.generate_tour", side_effect=_delete_during_generation),
    ):
        await run_job(job_id, WORKER_A)

    with Session(pg_engine_clean) as session:
        assert session.exec(select(Job)).all() == []


@contextlib.contextmanager
def _brief_worker(engine, generate):
    """Point run_job at ``engine`` and stub GitHub and the LLM so it reaches
    generate_brief."""
    with contextlib.ExitStack() as stack:
        for item in (
            patch("app.worker.engine", engine),
            patch(
                "app.worker.fetch_issue_thread",
                return_value=MagicMock(branch_instruction=None),
            ),
            patch(
                "app.worker.resolve_target_branch",
                return_value=MagicMock(branch="main", default_branch="main"),
            ),
            patch("app.worker.resolve_fork_status", return_value=MagicMock()),
            patch("app.worker.generate_brief", generate),
            patch("app.worker._charge_refresh_ingest", return_value=True),
        ):
            stack.enter_context(item)
        yield


def _running_issue_brief(engine) -> int:
    with Session(engine) as session:
        brief_id = _insert_brief(session).id
        row = session.get(Job, brief_id)
        row.issue_repo = "org/repo"
        row.issue_number = 44
        session.commit()
        assert claim_next_job(session, WORKER_A) == brief_id
    return brief_id


@pytest.mark.parametrize("outcome", ["drafted", "needs_refresh"])
async def test_worker_discards_brief_deleted_during_generation(
    pg_engine_clean, outcome
):
    """A brief deleted while generate_brief runs is neither written back nor
    parked, and a refresh it asked for leaves no shared ingest behind."""
    job_id = _running_issue_brief(pg_engine_clean)
    reached = []

    async def _delete_during_generation(*_args, **_kwargs):
        reached.append(True)
        await _delete(pg_engine_clean, job_id)
        if outcome == "needs_refresh":
            raise BriefNeedsRefreshError("stale")
        return MagicMock(model_dump=MagicMock(return_value={"summary": "late"}))

    with _brief_worker(
        pg_engine_clean, AsyncMock(side_effect=_delete_during_generation)
    ):
        await run_job(job_id, WORKER_A)

    assert reached == [True]
    with Session(pg_engine_clean) as session:
        assert session.exec(select(Job)).all() == []


def _waiting_brief(session: Session, shared: Job, user_id: str, installation_id: int) -> Job:
    return _insert_brief(
        session,
        userId=user_id,
        installation_id=installation_id,
        dedupe_key=f"{JobType.ISSUE_BRIEF}:{user_id}:org/repo:main:org/repo:44",
        blocked_by_job_id=shared.id,
    )


async def test_shared_ingest_stays_claimable_while_another_brief_waits(
    pg_engine_clean,
):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session)
        deleted = _waiting_brief(session, shared, OWNER, 101)
        teammate = _waiting_brief(session, shared, "teammate", 202)

    await _delete(pg_engine_clean, deleted.id)

    assert not _exists(pg_engine_clean, deleted.id)
    assert _exists(pg_engine_clean, teammate.id)
    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) == shared.id


async def test_shared_ingest_idles_after_last_waiter_is_deleted_and_revives(
    pg_engine_clean,
):
    with Session(pg_engine_clean) as session:
        shared = _insert_shared_ingest(session)
        deleted = _waiting_brief(session, shared, OWNER, 101)

    await _delete(pg_engine_clean, deleted.id)

    with Session(pg_engine_clean) as session:
        assert claim_next_job(session, WORKER_A) is None
    idle = _reload(pg_engine_clean, shared.id)
    assert idle.status == JobStatus.PENDING
    assert idle.attempts == 0

    with Session(pg_engine_clean) as session:
        _waiting_brief(session, shared, "newcomer", 303)
        assert claim_next_job(session, WORKER_A) == shared.id
