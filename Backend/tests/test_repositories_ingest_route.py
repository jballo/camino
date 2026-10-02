from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

from app.db import get_session
from app.main import app
from app.models.code import RepoIndexState
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
    job.blocked_by_job_id = overrides.get("blocked_by_job_id")
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


def test_post_returns_requesters_waiting_row_not_shared_ingest():
    shared = _job(id=15, userId=None, status=JobStatus.RUNNING)
    waiting = _job(id=21, status=JobStatus.PENDING, blocked_by_job_id=15)
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(INSTALLATION_ID, "public"),
        ),
        patch(
            "app.api.repositories.enqueue_shared_ingest",
            return_value=(waiting, shared, False),
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
    with patch(
        "app.api.repositories.cancel_job", side_effect=transition
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
    RepoIndexState.__table__.create(engine)
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


SHARED_KEY = "repository_ingest:org/repo:main"


def _shared_ingest(session: Session, **values) -> Job:
    shared = Job(
        userId=None,
        installation_id=None,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=SHARED_KEY,
        **values,
    )
    session.add(shared)
    session.flush()
    return shared


def _waiting_row(session: Session, shared: Job, user_id: str, installation_id: int) -> Job:
    waiting = Job(
        userId=user_id,
        installation_id=installation_id,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=f"{SHARED_KEY}:user:{user_id}",
        status=JobStatus.PENDING,
        blocked_by_job_id=shared.id,
    )
    session.add(waiting)
    session.commit()
    return waiting


def test_first_requester_gets_waiting_row_on_ownerless_shared_ingest(sqlite_session):
    session = sqlite_session
    with patch(
        "app.api.repositories.resolve_repo_access",
        return_value=RepoAccess(INSTALLATION_ID, "public"),
    ):
        response = client.post(URL, json={"repoName": "org/repo", "ref": "main"})

    assert response.status_code == 200
    waiting = session.get(Job, response.json()["id"])
    assert waiting.userId == USER_ID
    assert waiting.installation_id == INSTALLATION_ID
    shared = session.get(Job, waiting.blocked_by_job_id)
    assert shared.userId is None
    assert shared.dedupe_key == SHARED_KEY


def test_get_waiting_row_reports_shared_ingest_running_state(sqlite_session):
    session = sqlite_session
    shared = _shared_ingest(
        session, status=JobStatus.RUNNING, claimed_by="worker", attempts=1
    )
    waiting = _waiting_row(session, shared, USER_ID, INSTALLATION_ID)

    response = client.get(f"{URL}/{waiting.id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": waiting.id,
        "status": JobStatus.RUNNING,
        "repoName": "org/repo",
        "ref": "main",
        "attempts": 1,
        "result": None,
        "error": None,
    }
    assert session.get(Job, waiting.id).status == JobStatus.PENDING


@pytest.mark.parametrize(
    ("shared_values", "expected"),
    [
        (
            {"status": JobStatus.COMPLETE, "artifact": {"chunks_inserted": 4}},
            {
                "status": JobStatus.COMPLETE,
                "result": {"chunks_inserted": 4},
                "error": None,
            },
        ),
        (
            {"status": JobStatus.FAILED, "error": "Repository not found"},
            {
                "status": JobStatus.FAILED,
                "result": None,
                "error": "ingest failed: Repository not found",
            },
        ),
    ],
)
def test_get_waiting_row_reports_finished_shared_ingest_before_it_settles(
    sqlite_session, shared_values, expected
):
    session = sqlite_session
    shared = _shared_ingest(session, **shared_values)
    waiting = _waiting_row(session, shared, USER_ID, INSTALLATION_ID)

    response = client.get(f"{URL}/{waiting.id}")

    assert response.status_code == 200
    body = response.json()
    assert {key: body[key] for key in expected} == expected
    assert session.get(Job, waiting.id).status == JobStatus.PENDING


def test_cancel_rejects_shared_ingest(sqlite_session):
    session = sqlite_session
    shared = _shared_ingest(session, status=JobStatus.RUNNING, claimed_by="worker")
    session.commit()

    with patch(
        "app.api.repositories.resolve_repo_access",
        return_value=RepoAccess(INSTALLATION_ID, "public"),
    ):
        response = client.post(f"{URL}/{shared.id}/cancel")

    assert response.status_code == 403
    assert session.get(Job, shared.id).status == JobStatus.RUNNING


def test_cancel_waiting_row_leaves_shared_ingest_active(sqlite_session):
    session = sqlite_session
    shared = _shared_ingest(session, status=JobStatus.RUNNING, claimed_by="worker")
    own = _waiting_row(session, shared, USER_ID, INSTALLATION_ID)
    other = _waiting_row(session, shared, "other_user", 789)
    shared_id, own_id, other_id = shared.id, own.id, other.id

    response = client.post(f"{URL}/{own_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == JobStatus.CANCELLED
    shared = session.get(Job, shared_id)
    assert shared.status == JobStatus.RUNNING
    assert shared.userId is None
    assert shared.claimed_by == "worker"
    assert session.get(Job, other_id).status == JobStatus.PENDING
