from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.db import get_session
from app.main import app
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import REPOSITORY_INGEST_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.repo_access import RepoAccess, RepoAccessDenied

USER_ID = "user_123"
INSTALLATION_ID = 456
URL = "/api/v1/repositories/ingest"


def _session():
    session = MagicMock()
    connection = MagicMock(installationId=INSTALLATION_ID)
    session.exec.return_value.one.return_value = connection
    yield session


@pytest.fixture(autouse=True)
def _dependencies():
    app.dependency_overrides[get_authenticated_user_id] = lambda: USER_ID
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[REPOSITORY_INGEST_RATE_LIMIT] = lambda: None
    yield
    app.dependency_overrides.clear()


client = TestClient(app)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


def _job(**overrides):
    job = MagicMock()
    job.id = overrides.get("id", 12)
    job.userId = overrides.get("userId", USER_ID)
    job.installation_id = overrides.get("installation_id", INSTALLATION_ID)
    job.repo_name = overrides.get("repo_name", "org/repo")
    job.ref = overrides.get("ref", "main")
    job.job_type = overrides.get("job_type", JobType.REPOSITORY_INGEST)
    job.status = overrides.get("status", JobStatus.PENDING)
    job.attempts = overrides.get("attempts", 0)
    job.artifact = overrides.get("artifact")
    job.error = overrides.get("error")
    return job


def test_post_enqueues_repository_ingestion_job():
    job = _job()
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(INSTALLATION_ID, "public"),
        ),
        patch(
            "app.api.repositories.enqueue_shared_ingest",
            return_value=(job, job, True),
        ) as enqueue,
    ):
        response = client.post(
            URL, json={"repoName": "org/repo", "ref": "main"}
        )

    assert response.status_code == 200
    assert response.json() == {"id": 12, "status": JobStatus.PENDING}
    assert enqueue.call_args.kwargs == {
        "user_id": USER_ID,
        "installation_id": INSTALLATION_ID,
        "repo_name": "org/repo",
        "ref": "main",
        "waiting_row": True,
    }


def test_post_returns_owned_primary_for_same_user_duplicate_enqueue():
    job = _job(id=15, status=JobStatus.RUNNING)
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(INSTALLATION_ID, "public"),
        ),
        patch(
            "app.api.repositories.enqueue_shared_ingest",
            return_value=(job, job, False),
        ),
    ):
        response = client.post(
            URL, json={"repoName": "org/repo", "ref": "main"}
        )

    assert response.status_code == 200
    assert response.json() == {"id": 15, "status": JobStatus.RUNNING}


def test_post_returns_requesters_waiting_row_for_other_users_ingest():
    primary = _job(id=15, userId="other_user", status=JobStatus.RUNNING)
    waiting = _job(id=21, status=JobStatus.PENDING)
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(INSTALLATION_ID, "public"),
        ),
        patch(
            "app.api.repositories.enqueue_shared_ingest",
            return_value=(waiting, primary, True),
        ),
    ):
        response = client.post(
            URL, json={"repoName": "org/repo", "ref": "main"}
        )

    assert response.status_code == 200
    assert response.json() == {"id": 21, "status": JobStatus.PENDING}


def test_post_resolves_target_branch_when_ref_is_omitted():
    job = _job(ref="develop")
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(INSTALLATION_ID, "public"),
        ),
        patch(
            "app.api.repositories.resolve_target_branch",
            return_value=SimpleNamespace(branch="develop"),
        ) as resolve,
        patch(
            "app.api.repositories.enqueue_shared_ingest",
            return_value=(job, job, True),
        ) as enqueue,
    ):
        response = client.post(URL, json={"repoName": "org/repo"})

    assert response.status_code == 200
    resolve.assert_called_once_with("org/repo", INSTALLATION_ID)
    assert enqueue.call_args.kwargs["ref"] == "develop"


def test_post_denies_inaccessible_repository_before_enqueue():
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            side_effect=RepoAccessDenied("Repository not found"),
        ),
        patch("app.api.repositories.enqueue_shared_ingest") as enqueue,
    ):
        response = client.post(
            URL, json={"repoName": "org/private", "ref": "main"}
        )

    assert response.status_code == 404
    enqueue.assert_not_called()


def test_post_surfaces_revoked_installation_reconnect_error():
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            side_effect=RepoAccessDenied(
                "GitHub connection is no longer valid — reconnect"
            ),
        ),
        patch("app.api.repositories.enqueue_shared_ingest") as enqueue,
    ):
        response = client.post(
            URL,
            json={"repoName": "org/repo", "ref": "main"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "GitHub connection is no longer valid — reconnect"
    }
    enqueue.assert_not_called()


def test_get_returns_ingestion_status_and_result():
    job = _job(
        status=JobStatus.COMPLETE,
        attempts=2,
        artifact={"chunks_inserted": 10, "embeddings_created": 10},
    )

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        yield session

    app.dependency_overrides[get_session] = session_with_job
    response = client.get(f"{URL}/12")

    assert response.status_code == 200
    assert response.json() == {
        "id": 12,
        "status": JobStatus.COMPLETE,
        "repoName": "org/repo",
        "ref": "main",
        "attempts": 2,
        "result": {"chunks_inserted": 10, "embeddings_created": 10},
        "error": None,
    }


def test_get_rejects_a_different_job_type():
    job = _job(job_type=JobType.TOUR)

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        yield session

    app.dependency_overrides[get_session] = session_with_job
    response = client.get(f"{URL}/12")

    assert response.status_code == 404


def test_get_allows_non_owner_with_access_to_repository():
    job = _job(userId="other_user")
    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.one_or_none.return_value = None
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(INSTALLATION_ID, "public"),
        ),
    ):
        response = client.get(f"{URL}/12")

    assert response.status_code == 200
    assert response.json()["repoName"] == "org/repo"


def test_get_hides_non_owner_job_without_repository_access():
    job = _job(userId="other_user")
    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.one_or_none.return_value = None
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            side_effect=RepoAccessDenied("Repository not found"),
        ),
    ):
        response = client.get(f"{URL}/12")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ingestion job not found"}


def test_get_hides_non_owner_job_without_matching_installation():
    job = _job(userId="other_user")

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.one_or_none.return_value = None
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with patch(
        "app.api.repositories.resolve_repo_access",
        side_effect=RepoAccessDenied("Repository not found"),
    ) as access:
        response = client.get(f"{URL}/12")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ingestion job not found"}
    access.assert_called_once()


def test_cancel_rejects_non_owner_with_repository_access():
    job = _job(userId="other_user")
    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.one_or_none.return_value = None
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(INSTALLATION_ID, "public"),
        ),
        patch("app.api.repositories.cancel_shared_ingest") as cancel,
    ):
        response = client.post(f"{URL}/12/cancel")

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Only the job owner can cancel this ingestion"
    }
    cancel.assert_not_called()


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_cancel_transitions_active_ingestion_job(status):
    job = _job(status=status)

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        yield session

    def transition(_session, cancelled):
        assert cancelled is job
        job.status = JobStatus.CANCELLED

    app.dependency_overrides[get_session] = session_with_job
    with patch(
        "app.api.repositories.cancel_shared_ingest", side_effect=transition
    ) as cancel:
        response = client.post(f"{URL}/12/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == JobStatus.CANCELLED
    cancel.assert_called_once()


def test_cancel_is_idempotent_for_cancelled_ingestion_job():
    job = _job(status=JobStatus.CANCELLED)

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with patch("app.api.repositories.cancel_shared_ingest") as cancel:
        response = client.post(f"{URL}/12/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == JobStatus.CANCELLED
    cancel.assert_not_called()


@pytest.mark.parametrize("status", [JobStatus.COMPLETE, JobStatus.FAILED])
def test_cancel_rejects_terminal_ingestion_job(status):
    job = _job(status=status)

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        yield session

    app.dependency_overrides[get_session] = session_with_job
    response = client.post(f"{URL}/12/cancel")

    assert response.status_code == 409
    assert status in response.json()["detail"]


@pytest.mark.parametrize(
    "job",
    [
        None,
        _job(job_type=JobType.TOUR),
    ],
)
def test_cancel_hides_unknown_or_wrong_type_ingestion_job(job):
    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        yield session

    app.dependency_overrides[get_session] = session_with_job
    response = client.post(f"{URL}/12/cancel")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ingestion job not found"}


def test_cancel_hides_unauthorized_ingestion_job():
    job = _job(userId="other_user")

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.one_or_none.return_value = None
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with patch(
        "app.api.repositories.resolve_repo_access",
        side_effect=RepoAccessDenied("Repository not found"),
    ):
        response = client.post(f"{URL}/12/cancel")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ingestion job not found"}


@pytest.fixture
def sqlite_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    GithubConnections.__table__.create(engine)
    Job.__table__.create(engine)
    with Session(engine) as session:
        for index, (user_id, installation_id) in enumerate(
            [(USER_ID, INSTALLATION_ID), ("other_user", 789)]
        ):
            session.add(
                GithubConnections(
                    userId=user_id,
                    githubUsername=user_id,
                    githubUserId=index + 1,
                    installationId=installation_id,
                )
            )
        session.commit()

        def _session():
            yield session

        app.dependency_overrides[get_session] = _session
        yield session
    engine.dispose()


def _ingest_row(user_id: str, installation_id: int, **overrides) -> Job:
    return Job(
        userId=user_id,
        installation_id=installation_id,
        repo_name="org/repo",
        ref="main",
        job_type=overrides.get("job_type", JobType.REPOSITORY_INGEST),
        dedupe_key=overrides.get("dedupe_key", "repository_ingest:org/repo:main"),
        status=overrides.get("status", JobStatus.RUNNING),
        claimed_by="worker" if overrides.get("status") is None else None,
        blocked_by_job_id=overrides.get("blocked_by_job_id"),
    )


def test_owner_cancel_hands_ingest_to_other_users_waiting_brief(sqlite_session):
    session = sqlite_session
    primary = _ingest_row(USER_ID, INSTALLATION_ID)
    session.add(primary)
    session.flush()
    own_brief = _ingest_row(
        USER_ID,
        INSTALLATION_ID,
        job_type=JobType.ISSUE_BRIEF,
        dedupe_key=None,
        status=JobStatus.PENDING,
        blocked_by_job_id=primary.id,
    )
    other_brief = _ingest_row(
        "other_user",
        789,
        job_type=JobType.ISSUE_BRIEF,
        dedupe_key=None,
        status=JobStatus.PENDING,
        blocked_by_job_id=primary.id,
    )
    session.add_all([own_brief, other_brief])
    session.commit()
    primary_id, own_brief_id, other_brief_id = (
        primary.id,
        own_brief.id,
        other_brief.id,
    )

    response = client.post(f"{URL}/{primary_id}/cancel")

    assert response.status_code == 200
    assert response.json()["id"] == primary_id
    assert response.json()["status"] == JobStatus.CANCELLED
    replacement = session.exec(
        select(Job).where(
            Job.dedupe_key == "repository_ingest:org/repo:main",
            Job.status == JobStatus.PENDING,
        )
    ).one()
    assert replacement.userId == "other_user"
    assert replacement.installation_id == 789
    other_brief = session.get(Job, other_brief_id)
    assert other_brief.status == JobStatus.PENDING
    assert other_brief.blocked_by_job_id == replacement.id
    assert session.get(Job, own_brief_id).blocked_by_job_id == primary_id


def test_owner_cancel_without_other_users_cancels_ingest(sqlite_session):
    session = sqlite_session
    primary = _ingest_row(USER_ID, INSTALLATION_ID, status=JobStatus.PENDING)
    session.add(primary)
    session.commit()
    primary_id = primary.id

    response = client.post(f"{URL}/{primary_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == JobStatus.CANCELLED
    assert session.exec(select(Job)).all() == [session.get(Job, primary_id)]


def test_waiting_row_owner_cancel_leaves_primary_active(sqlite_session):
    session = sqlite_session
    primary = _ingest_row("other_user", 789)
    session.add(primary)
    session.flush()
    waiting = _ingest_row(
        USER_ID,
        INSTALLATION_ID,
        dedupe_key=f"repository_ingest:org/repo:main:user:{USER_ID}",
        status=JobStatus.PENDING,
        blocked_by_job_id=primary.id,
    )
    session.add(waiting)
    session.commit()
    primary_id, waiting_id = primary.id, waiting.id

    response = client.post(f"{URL}/{waiting_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == JobStatus.CANCELLED
    primary = session.get(Job, primary_id)
    assert primary.status == JobStatus.RUNNING
    assert primary.userId == "other_user"
    assert primary.claimed_by == "worker"
