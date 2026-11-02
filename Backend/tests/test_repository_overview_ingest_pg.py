"""The Explore overview reports each repository's latest index request.

Runs against a real Postgres so the overview's job query, the shared-ingest
dedupe and the waiting-row settlement all behave as they do in the API. GitHub
is patched out: the user's installation lists ``org/repo`` and the index
summary is empty, so every entry is an installed, not yet indexed repository.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text, update
from sqlmodel import Session

from app.api import repositories
from app.api.repositories import repository_overview
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus
from app.services.installation_state import SUSPENSION_ERROR
from app.services.repository_ingestion import _PUBLISH_GENERATION_SQL
from app.services.shared_ingests import enqueue_shared_ingest
from tests.test_worker_claim_pg import pg_engine  # noqa: F401  (fixture)


USER_ID = "user_overview"
OTHER_USER_ID = "user_bystander"
REPO = "org/repo"
INSTALLATION_ID = 12


@pytest.fixture
def engine(pg_engine):  # noqa: F811
    with Session(pg_engine) as session:
        session.execute(
            text(
                "TRUNCATE jobs, repo_index_state, user_repo_follows, "
                "code_chunks, githubconnections RESTART IDENTITY CASCADE"
            )
        )
        for user_id in (USER_ID, OTHER_USER_ID):
            session.add(
                GithubConnections(
                    userId=user_id,
                    githubUsername=f"github-{user_id}",
                    githubUserId=uuid.uuid4().int % 2_000_000_000,
                    installationId=INSTALLATION_ID,
                )
            )
        session.commit()
    return pg_engine


def _request(session: Session, user_id: str = USER_ID) -> tuple[Job, Job]:
    waiting, shared, _ = enqueue_shared_ingest(
        session,
        user_id=user_id,
        installation_id=INSTALLATION_ID,
        repo_name=REPO,
        ref="main",
    )
    return waiting, shared


def _set_job(session: Session, job_id: int, **values) -> None:
    session.execute(update(Job).where(Job.id == job_id).values(**values))
    session.commit()


async def _overview_entry(session: Session, user_id: str = USER_ID):
    with (
        patch(
            "app.api.repositories._installed_repository_names",
            return_value={REPO},
        ),
        patch("app.api.repositories._index_rows", return_value=[]),
    ):
        overview = await repository_overview(session, user_id)
    assert [entry.repoName for entry in overview.installed] == [REPO]
    return overview.installed[0]


@pytest.mark.asyncio
async def test_unsettled_waiting_row_reports_the_running_shared_ingest(engine):
    with Session(engine) as session:
        waiting, shared = _request(session)
        _set_job(session, shared.id, status=JobStatus.RUNNING)

        entry = await _overview_entry(session)

    assert entry.ingest is not None
    assert entry.ingest.id == waiting.id
    assert entry.ingest.status == JobStatus.RUNNING
    assert entry.ingest.ref == "main"
    assert entry.ingest.error is None


@pytest.mark.asyncio
async def test_finished_shared_ingest_reads_complete_before_the_row_settles(
    engine,
):
    with Session(engine) as session:
        waiting, shared = _request(session)
        _set_job(session, shared.id, status=JobStatus.COMPLETE)

        entry = await _overview_entry(session)
        session.refresh(waiting)

    assert waiting.status == JobStatus.PENDING
    assert entry.ingest.status == JobStatus.COMPLETE
    assert entry.ingest.error is None


@pytest.mark.asyncio
async def test_suspended_requester_sees_their_cancelled_row_and_reason(engine):
    with Session(engine) as session:
        waiting, _ = _request(session)
        _set_job(
            session,
            waiting.id,
            status=JobStatus.CANCELLED,
            error=SUSPENSION_ERROR,
        )

        entry = await _overview_entry(session)

    assert entry.ingest.id == waiting.id
    assert entry.ingest.status == JobStatus.CANCELLED
    assert entry.ingest.error == SUSPENSION_ERROR


@pytest.mark.asyncio
async def test_bystander_sees_a_failed_shared_ingest_as_failed(engine):
    with Session(engine) as session:
        _, shared = _request(session)
        _set_job(
            session,
            shared.id,
            status=JobStatus.FAILED,
            error="Exceeded max attempts: clone timed out",
        )

        entry = await _overview_entry(session)

    assert entry.ingest.status == JobStatus.FAILED
    assert entry.ingest.error == (
        "ingest failed: Exceeded max attempts: clone timed out"
    )


@pytest.mark.asyncio
async def test_a_new_request_replaces_the_failed_one(engine):
    with Session(engine) as session:
        first, shared = _request(session)
        _set_job(session, shared.id, status=JobStatus.FAILED, error="boom")
        second, _ = _request(session)

        entry = await _overview_entry(session)
        # Read while attached: the overview no longer loads the superseded
        # row, so nothing else refreshes `first` after the commits above.
        first_id, second_id = first.id, second.id

    assert second_id != first_id
    assert entry.ingest.id == second_id
    assert entry.ingest.status == JobStatus.PENDING
    assert entry.ingest.error is None


@pytest.mark.asyncio
async def test_other_users_requests_and_shared_ingests_are_not_reported(engine):
    with Session(engine) as session:
        _request(session, user_id=OTHER_USER_ID)

        entry = await _overview_entry(session)

    assert entry.ingest is None


@pytest.mark.asyncio
async def test_index_published_mid_overview_is_never_complete_without_refs(
    engine,
):
    """The worker's publish+complete commit lands between the overview's reads.

    Whatever the request state says, an index the worker has published by the
    time the index rows are read must be in the response. A "complete" request
    beside no refs would leave Explore on "Index ready. Loading it…" for good.
    """
    with Session(engine) as session:
        _, shared = _request(session)
        _set_job(session, shared.id, status=JobStatus.RUNNING)
        read_latest_ingests = repositories._latest_ingests

        def read_then_worker_commits(*args, **kwargs):
            jobs = read_latest_ingests(*args, **kwargs)
            with Session(engine) as worker:
                worker.execute(
                    _PUBLISH_GENERATION_SQL,
                    {
                        "repo_name": REPO,
                        "ref": "main",
                        "visibility": "public",
                        "generation": "gen-1",
                        "commit_sha": "abc123",
                    },
                )
                worker.execute(
                    update(Job)
                    .where(Job.id == shared.id)
                    .values(status=JobStatus.COMPLETE)
                )
                worker.commit()
            return jobs

        with (
            patch(
                "app.api.repositories._installed_repository_names",
                return_value={REPO},
            ),
            patch(
                "app.api.repositories._latest_ingests",
                side_effect=read_then_worker_commits,
            ),
        ):
            overview = await repository_overview(session, USER_ID)

    entry = overview.installed[0]
    assert [ref.ref for ref in entry.refs] == ["main"]
    assert entry.refs[0].indexedSha == "abc123"
    assert entry.ingest is not None
    assert entry.ingest.status in {JobStatus.RUNNING, JobStatus.COMPLETE}
