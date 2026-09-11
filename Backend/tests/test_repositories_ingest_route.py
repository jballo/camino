from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.models.job import JobStatus, JobType
from app.rate_limit import REPOSITORY_INGEST_RATE_LIMIT
from app.security import get_authenticated_user_id

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


def _job(**overrides):
    job = MagicMock()
    job.id = overrides.get("id", 12)
    job.userId = overrides.get("userId", USER_ID)
    job.installation_id = overrides.get("installation_id", INSTALLATION_ID)
    job.repo_name = overrides.get("repo_name", "org/repo")
    job.job_type = overrides.get("job_type", JobType.REPOSITORY_INGEST)
    job.status = overrides.get("status", JobStatus.PENDING)
    job.attempts = overrides.get("attempts", 0)
    job.artifact = overrides.get("artifact")
    job.error = overrides.get("error")
    return job


def test_post_enqueues_repository_ingestion_job():
    job = _job()
    with patch(
        "app.api.repositories.enqueue_job",
        return_value=(job, True),
    ) as enqueue:
        response = client.post(URL, json={"repoName": "org/repo"})

    assert response.status_code == 200
    assert response.json() == {"id": 12, "status": JobStatus.PENDING}
    assert enqueue.call_args.kwargs == {
        "user_id": USER_ID,
        "installation_id": INSTALLATION_ID,
        "repo_name": "org/repo",
        "job_type": JobType.REPOSITORY_INGEST,
        "dedupe_key": "repository_ingest:456:org/repo",
    }


def test_post_returns_existing_active_job_for_duplicate_enqueue():
    job = _job(id=15, status=JobStatus.RUNNING)
    with patch(
        "app.api.repositories.enqueue_job",
        return_value=(job, False),
    ):
        response = client.post(URL, json={"repoName": "org/repo"})

    assert response.status_code == 200
    assert response.json() == {"id": 15, "status": JobStatus.RUNNING}


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
    connection = MagicMock(installationId=INSTALLATION_ID)
    installation = MagicMock()
    installation.get_repos.return_value = [
        MagicMock(full_name="ORG/REPO"),
    ]
    integration = MagicMock()
    integration.get_app_installation.return_value = installation

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.first.return_value = connection
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with (
        patch("app.api.repositories.Auth.AppAuth", return_value=MagicMock()),
        patch(
            "app.api.repositories.GithubIntegration",
            return_value=integration,
        ),
    ):
        response = client.get(f"{URL}/12")

    assert response.status_code == 200
    assert response.json()["repoName"] == "org/repo"
    integration.get_app_installation.assert_called_once_with(INSTALLATION_ID)


def test_get_hides_non_owner_job_without_repository_access():
    job = _job(userId="other_user")
    connection = MagicMock(installationId=INSTALLATION_ID)
    installation = MagicMock()
    installation.get_repos.return_value = [
        MagicMock(full_name="org/different-repo"),
    ]
    integration = MagicMock()
    integration.get_app_installation.return_value = installation

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.first.return_value = connection
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with (
        patch("app.api.repositories.Auth.AppAuth", return_value=MagicMock()),
        patch(
            "app.api.repositories.GithubIntegration",
            return_value=integration,
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
        session.exec.return_value.first.return_value = None
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with patch("app.api.repositories.GithubIntegration") as integration:
        response = client.get(f"{URL}/12")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ingestion job not found"}
    integration.assert_not_called()


def test_cancel_rejects_non_owner_with_repository_access():
    job = _job(userId="other_user")
    connection = MagicMock(installationId=INSTALLATION_ID)
    installation = MagicMock()
    installation.get_repos.return_value = [
        MagicMock(full_name="ORG/REPO"),
    ]
    integration = MagicMock()
    integration.get_app_installation.return_value = installation

    def session_with_job():
        session = MagicMock()
        session.get.return_value = job
        session.exec.return_value.first.return_value = connection
        yield session

    app.dependency_overrides[get_session] = session_with_job
    with (
        patch("app.api.repositories.Auth.AppAuth", return_value=MagicMock()),
        patch(
            "app.api.repositories.GithubIntegration",
            return_value=integration,
        ),
        patch("app.api.repositories.cancel_job") as cancel,
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

    def transition(_session, job_id):
        assert job_id == job.id
        job.status = JobStatus.CANCELLED
        return True

    app.dependency_overrides[get_session] = session_with_job
    with patch("app.api.repositories.cancel_job", side_effect=transition) as cancel:
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
    with patch("app.api.repositories.cancel_job") as cancel:
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
        session.exec.return_value.first.return_value = None
        yield session

    app.dependency_overrides[get_session] = session_with_job
    response = client.post(f"{URL}/12/cancel")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ingestion job not found"}
