"""Overlapping follows of one repository must not freeze the event loop.

Reproduces the deadlock from the review of PR #101 against a real Postgres,
because only Postgres makes a conflicting INSERT wait on another transaction's
uncommitted row. Both follow handlers run on one event loop thread, as they do
in the API process.

Request A writes its follow and job rows, then charges the ingest limit before
committing. The fake limiter holds A there until request B is blocked in
Postgres on A's rows. If A's write block ran on the event loop, B's blocked
call would hold the loop and A could never resume to commit. With the write
block on a worker thread, A commits, Postgres releases B, and both finish.

If the deadlock returns, the test terminates the stuck database connections so
the loop thread can exit, then fails.
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.api.repositories import (
    REPOSITORY_INGEST_RATE_LIMIT,
    RepoFollowBody,
    follow_repository,
)
from app.models.job import Job
from app.models.repo_follow import UserRepoFollow
from app.services.repo_access import RepoAccess
from tests.test_worker_claim_pg import pg_engine  # noqa: F401  (fixture)


USER_ID = "user_overlap"
REPO = "org/overlap"
DEADLOCK_TIMEOUT_SECONDS = 10


@pytest.fixture
def engine(pg_engine):  # noqa: F811
    UserRepoFollow.__table__.create(pg_engine, checkfirst=True)
    with Session(pg_engine) as session:
        session.execute(
            text(
                "TRUNCATE jobs, repo_index_state, user_repo_follows "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()
    return pg_engine


@pytest.fixture
def loop():
    """One event loop on its own thread, like the API process's loop."""
    event_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=event_loop.run_forever, daemon=True)
    thread.start()
    yield event_loop
    event_loop.call_soon_threadsafe(event_loop.stop)
    thread.join(timeout=5)
    event_loop.close()


def _blocked_on_lock(engine) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock'"
                )
            ).scalar()
        )


def _terminate_other_connections(engine) -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )


def test_overlapping_follows_of_one_repository_both_finish(engine, loop):
    a_in_limiter = threading.Event()
    release_a = threading.Event()
    checks = []

    def fake_check(user_id):
        checks.append(user_id)
        a_in_limiter.set()
        assert release_a.wait(DEADLOCK_TIMEOUT_SECONDS)

    async def follow():
        with Session(engine) as session:
            return await follow_repository(
                RepoFollowBody(repoName=REPO),
                session,
                USER_ID,
            )

    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(installation_id=12, visibility="public"),
        ),
        patch(
            "app.api.repositories._installed_repository_names",
            return_value=set(),
        ),
        patch(
            "app.api.repositories.resolve_target_branch",
            return_value=SimpleNamespace(branch="main"),
        ),
        patch.object(REPOSITORY_INGEST_RATE_LIMIT, "check", side_effect=fake_check),
    ):
        request_a = asyncio.run_coroutine_threadsafe(follow(), loop)
        assert a_in_limiter.wait(DEADLOCK_TIMEOUT_SECONDS)

        request_b = asyncio.run_coroutine_threadsafe(follow(), loop)
        deadline = time.monotonic() + DEADLOCK_TIMEOUT_SECONDS
        while not _blocked_on_lock(engine):
            assert time.monotonic() < deadline, "B never blocked on A's rows"
            time.sleep(0.05)
        release_a.set()

        try:
            result_a = request_a.result(timeout=DEADLOCK_TIMEOUT_SECONDS)
            result_b = request_b.result(timeout=DEADLOCK_TIMEOUT_SECONDS)
        except TimeoutError:
            _terminate_other_connections(engine)
            pytest.fail("Overlapping follows deadlocked the event loop")

    assert result_a.jobQueued is True
    assert result_b.jobQueued is False
    assert checks == [USER_ID]
    with Session(engine) as session:
        jobs = session.exec(select(Job).order_by(Job.id)).all()
        follows = session.exec(select(UserRepoFollow)).all()
    assert [job.userId for job in jobs] == [None, USER_ID]
    assert jobs[1].blocked_by_job_id == jobs[0].id
    assert len(follows) == 1
