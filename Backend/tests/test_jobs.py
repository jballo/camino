from unittest.mock import MagicMock

from sqlalchemy import exc

from app.models.job import JobStatus, JobType
from app.services.jobs import (
    cancel_job,
    enqueue_job,
    issue_brief_dedupe_key,
    normalize_repository_name,
    repository_ingest_dedupe_key,
)


def test_issue_brief_identity_is_per_user():
    first = issue_brief_dedupe_key(
        user_id="user_1",
        repo_name="Org/Repo",
        ref="main",
        issue_repo="Contributor/Repo",
        issue_number=44,
    )
    same = issue_brief_dedupe_key(
        user_id="user_1",
        repo_name="org/repo",
        ref="main",
        issue_repo="contributor/repo",
        issue_number=44,
    )
    other_user = issue_brief_dedupe_key(
        user_id="user_2",
        repo_name="org/repo",
        ref="main",
        issue_repo="contributor/repo",
        issue_number=44,
    )
    assert first == same
    assert first != other_user


def test_issue_brief_identity_includes_issue_repository():
    fork_issue = issue_brief_dedupe_key(
        user_id="user_1",
        repo_name="org/repo",
        ref="main",
        issue_repo="contributor/repo",
        issue_number=44,
    )
    upstream_issue = issue_brief_dedupe_key(
        user_id="user_1",
        repo_name="org/repo",
        ref="main",
        issue_repo="org/repo",
        issue_number=44,
    )

    assert fork_issue != upstream_issue


def _enqueue(session: MagicMock):
    return enqueue_job(
        session,
        user_id="user_1",
        installation_id=123,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=repository_ingest_dedupe_key(
            repo_name="org/repo",
            ref="main",
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
    assert job.dedupe_key == "repository_ingest:org/repo:main"
    assert job.ref == "main"
    session.add.assert_called_once_with(job)
    session.commit.assert_called_once_with()


def test_enqueue_can_stage_job_in_callers_transaction():
    session = MagicMock()
    session.exec.return_value.first.return_value = None

    job, created = enqueue_job(
        session,
        user_id="user_1",
        installation_id=123,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key="repository_ingest:org/repo:main",
        commit=False,
    )

    assert created is True
    session.begin_nested.assert_called_once_with()
    session.add.assert_called_once_with(job)
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_staged_enqueue_dedupe_race_preserves_callers_transaction():
    session = MagicMock()
    winner = MagicMock(id=9, status=JobStatus.PENDING)
    first_lookup = MagicMock()
    first_lookup.first.return_value = None
    second_lookup = MagicMock()
    second_lookup.first.return_value = winner
    session.exec.side_effect = [first_lookup, second_lookup]
    session.flush.side_effect = exc.IntegrityError(
        "INSERT INTO jobs",
        {},
        Exception("duplicate key"),
    )

    job, created = enqueue_job(
        session,
        user_id="user_1",
        installation_id=123,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key="repository_ingest:org/repo:main",
        commit=False,
    )

    assert job is winner
    assert created is False
    session.begin_nested.assert_called_once_with()
    session.rollback.assert_not_called()
    session.commit.assert_not_called()


def test_repository_ingestion_identity_is_case_insensitive():
    assert normalize_repository_name("Org/Repo") == "org/repo"
    assert repository_ingest_dedupe_key(
        repo_name="Org/Repo",
        ref="main",
    ) == repository_ingest_dedupe_key(
        repo_name="org/repo",
        ref="main",
    )

    session = MagicMock()
    session.exec.return_value.first.return_value = None
    job, created = enqueue_job(
        session,
        user_id="user_1",
        installation_id=123,
        repo_name="Org/Repo",
        ref="main",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=repository_ingest_dedupe_key(
            repo_name="Org/Repo",
            ref="main",
        ),
    )

    assert created is True
    assert job.repo_name == "org/repo"
    assert job.dedupe_key == "repository_ingest:org/repo:main"


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


def test_cancel_job_cancels_pending_job_and_clears_claim():
    session = MagicMock()
    session.exec.return_value.rowcount = 1

    transitioned = cancel_job(session, 17)

    assert transitioned is True
    statement = session.exec.call_args.args[0]
    compiled = statement.compile()
    assert compiled.params["id_1"] == 17
    assert compiled.params["status"] == JobStatus.CANCELLED
    assert compiled.params["claimed_at"] is None
    assert compiled.params["claimed_by"] is None
    assert set(compiled.params["status_1"]) == set(JobStatus.ACTIVE)
    session.commit.assert_called_once_with()


def test_cancel_job_cancels_running_job_with_one_atomic_update():
    session = MagicMock()
    session.exec.return_value.rowcount = 1

    assert cancel_job(session, 18) is True

    statement = session.exec.call_args.args[0]
    assert str(statement).startswith("UPDATE jobs SET")
    assert session.exec.call_count == 1
    session.commit.assert_called_once_with()


def test_cancel_job_returns_false_for_terminal_job_and_still_commits_once():
    session = MagicMock()
    session.exec.return_value.rowcount = 0

    assert cancel_job(session, 19) is False

    session.exec.assert_called_once()
    session.commit.assert_called_once_with()
