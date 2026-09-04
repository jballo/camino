from unittest.mock import MagicMock

from sqlalchemy import exc

from app.models.job import JobStatus, JobType
from app.services.jobs import (
    enqueue_job,
    repository_ingest_dedupe_key,
)


def _enqueue(session: MagicMock):
    return enqueue_job(
        session,
        user_id="user_1",
        installation_id=123,
        repo_name="org/repo",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=repository_ingest_dedupe_key(
            installation_id=123,
            repo_name="org/repo",
        ),
    )


def test_enqueue_returns_existing_active_job():
    session = MagicMock()
    existing = MagicMock(id=7, status=JobStatus.RUNNING)
    session.exec.return_value.first.return_value = existing

    job, created = _enqueue(session)

    assert job is existing
    assert created is False
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_enqueue_creates_job_when_no_active_match_exists():
    session = MagicMock()
    session.exec.return_value.first.return_value = None

    def assign_id(job):
        job.id = 8

    session.refresh.side_effect = assign_id

    job, created = _enqueue(session)

    assert created is True
    assert job.id == 8
    assert job.status == JobStatus.PENDING
    assert job.job_type == JobType.REPOSITORY_INGEST
    assert job.dedupe_key == "repository_ingest:123:org/repo"
    session.add.assert_called_once_with(job)
    session.commit.assert_called_once_with()


def test_enqueue_recovers_concurrent_unique_index_loser():
    session = MagicMock()
    winner = MagicMock(id=9, status=JobStatus.PENDING)
    first_lookup = MagicMock()
    first_lookup.first.return_value = None
    second_lookup = MagicMock()
    second_lookup.first.return_value = winner
    session.exec.side_effect = [first_lookup, second_lookup]
    session.commit.side_effect = exc.IntegrityError(
        "INSERT INTO jobs",
        {},
        Exception("duplicate key"),
    )

    job, created = _enqueue(session)

    assert job is winner
    assert created is False
    session.rollback.assert_called_once_with()
