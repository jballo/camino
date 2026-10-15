"""A brief refused by the ingest limit saves nothing, against real Postgres (#100).

A repeat of a still-active brief whose ingest failed starts and charges no new
ingest (#104).

``test_briefs_rate_limit.py`` proves the route rolls back with a mocked
session. These tests run ``create_brief`` with a real session, the real
shared-ingest and job enqueue (including their savepoints), and query
``jobs`` afterwards. Only the GitHub preview and the rate-limit counter are
replaced.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text, update
from sqlmodel import Session, select

from app.api.briefs import BriefRequest, create_brief
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import REPOSITORY_INGEST_RATE_LIMIT_DETAIL, RateLimitDecision
from tests.test_briefs_rate_limit import _preview
from tests.test_worker_claim_pg import pg_engine  # noqa: F401  (fixture)


USER_ID = "user_rollback"
REPO = "org/unindexed"


@pytest.fixture
def engine(pg_engine):  # noqa: F811
    with Session(pg_engine) as session:
        session.execute(
            text("TRUNCATE jobs, repo_index_state RESTART IDENTITY CASCADE")
        )
        session.commit()
    return pg_engine


async def _create(engine, *, consume):
    with (
        patch(
            "app.api.briefs._preview",
            new=AsyncMock(return_value=(_preview(REPO), 12)),
        ),
        patch("app.rate_limit.consume_fixed_window", consume),
    ):
        with Session(engine) as session:
            return await create_brief(
                BriefRequest(issueUrl=f"https://github.com/{REPO}/issues/44"),
                session,
                USER_ID,
            )


def _saved_jobs(engine) -> list[tuple[str, str | None]]:
    with Session(engine) as session:
        return [
            (job.job_type, job.userId)
            for job in session.exec(select(Job).order_by(Job.id)).all()
        ]


async def test_allowed_brief_saves_its_ingest_and_brief(engine):
    """Control: the setup below does save rows when the limit allows it."""
    created = await _create(
        engine,
        consume=lambda **_: RateLimitDecision(allowed=True, retry_after=60),
    )

    assert created.id is not None
    assert _saved_jobs(engine) == [
        (JobType.REPOSITORY_INGEST, None),
        (JobType.ISSUE_BRIEF, USER_ID),
    ]


async def test_brief_refused_by_the_ingest_limit_saves_nothing(engine):
    with pytest.raises(HTTPException) as refused:
        await _create(
            engine,
            consume=lambda **_: RateLimitDecision(allowed=False, retry_after=60),
        )

    assert refused.value.status_code == 429
    assert refused.value.detail == REPOSITORY_INGEST_RATE_LIMIT_DETAIL
    assert _saved_jobs(engine) == []


async def test_brief_saves_nothing_when_the_limiter_is_unavailable(engine):
    def unavailable(**_):
        raise HTTPException(status_code=503, detail="Rate limit service unavailable")

    with pytest.raises(HTTPException) as failed:
        await _create(engine, consume=unavailable)

    assert failed.value.status_code == 503
    assert _saved_jobs(engine) == []


async def test_repeat_brief_after_its_ingest_failed_starts_and_charges_nothing(
    engine,
):
    """The ingest failed, but no worker has settled the brief yet."""
    first = await _create(
        engine,
        consume=lambda **_: RateLimitDecision(allowed=True, retry_after=60),
    )
    with Session(engine) as session:
        session.execute(
            update(Job)
            .where(Job.job_type == JobType.REPOSITORY_INGEST)
            .values(status=JobStatus.FAILED, error="clone failed")
        )
        session.commit()
    consume = MagicMock(return_value=RateLimitDecision(allowed=True, retry_after=60))

    repeat = await _create(engine, consume=consume)

    assert repeat.id == first.id
    consume.assert_not_called()
    assert _saved_jobs(engine) == [
        (JobType.REPOSITORY_INGEST, None),
        (JobType.ISSUE_BRIEF, USER_ID),
    ]
