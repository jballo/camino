import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from psycopg2.errorcodes import NOT_NULL_VIOLATION, UNIQUE_VIOLATION
from github import GithubException
from sqlalchemy.exc import IntegrityError

from app.db import get_session
from app.main import app
from app.models.github_connection import GithubConnections
from app.security import get_authenticated_user_id


CONNECT_URL = "/api/v1/github/connect"
GITHUB_USER_ID = 4242


def _noop_verify():
    return "user_123"


def _access_token():
    token = MagicMock()
    token.token = "access-token"
    token.expires_in = 28800
    token.refresh_token = "refresh-token"
    token.refresh_expires_in = 15897600
    token.created = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)
    return token


def _patch_github(
    installation_ids: tuple[int, ...] = (99,),
    *,
    installations_error: Exception | None = None,
    suspended_at: dt.datetime | None = None,
):
    oauth_app = MagicMock()
    oauth_app.get_access_token.return_value = _access_token()

    github_user = MagicMock()
    github_user.login = "octocat"
    github_user.id = GITHUB_USER_ID
    if installations_error is None:
        github_user.get_installations.return_value = [
            SimpleNamespace(id=installation_id, suspended_at=suspended_at)
            for installation_id in installation_ids
        ]
    else:
        github_user.get_installations.side_effect = installations_error

    anonymous = MagicMock()
    anonymous.get_oauth_application.return_value = oauth_app

    authenticated = MagicMock()
    authenticated.get_user.return_value = github_user

    return patch("app.api.github.Github", side_effect=[anonymous, authenticated])


@pytest.fixture
def client_and_session():
    session = MagicMock()

    def _session():
        yield session

    app.dependency_overrides[get_authenticated_user_id] = _noop_verify
    app.dependency_overrides[get_session] = _session
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _integrity_error(pgcode: str) -> IntegrityError:
    orig = Exception("constraint violated")
    orig.pgcode = pgcode
    return IntegrityError("INSERT", {}, orig)


def test_connect_persists_github_user_id(client_and_session):
    client, session = client_and_session
    session.exec.return_value.one_or_none.return_value = None

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    added = session.add.call_args.args[0]
    assert isinstance(added, GithubConnections)
    assert added.userId == "user_123"
    assert added.githubUsername == "octocat"
    assert added.githubUserId == GITHUB_USER_ID
    assert added.installationId == 99
    assert added.active is True


def test_connect_updates_github_user_id_on_existing_row(client_and_session):
    client, session = client_and_session
    existing = MagicMock()
    session.exec.return_value.one_or_none.return_value = existing

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    assert existing.githubUserId == GITHUB_USER_ID
    assert existing.githubUsername == "octocat"
    assert existing.installationId == 99
    assert existing.active is True


@pytest.mark.parametrize("existing", [False, True])
def test_connect_marks_suspended_installation_inactive(
    existing,
    client_and_session,
):
    client, session = client_and_session
    existing_connection = MagicMock() if existing else None
    session.exec.return_value.one_or_none.return_value = existing_connection

    with _patch_github(suspended_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    connection = existing_connection or session.add.call_args.args[0]
    assert connection.active is False


def test_unique_user_conflict_returns_409(client_and_session):
    client, session = client_and_session
    session.exec.return_value.one_or_none.return_value = None
    session.commit.side_effect = _integrity_error(UNIQUE_VIOLATION)

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Already connected"}
    session.rollback.assert_called_once_with()


def test_not_null_integrity_error_returns_500(client_and_session):
    client, session = client_and_session
    session.exec.return_value.one_or_none.return_value = None
    session.commit.side_effect = _integrity_error(NOT_NULL_VIOLATION)

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Database error"}
    session.rollback.assert_called_once_with()


def test_foreign_installation_is_rejected_without_persisting(client_and_session):
    client, session = client_and_session

    with _patch_github((12, 34)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Installation not accessible to this GitHub user"
    }
    session.exec.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_foreign_installation_cannot_replace_existing_connection(
    client_and_session,
):
    client, session = client_and_session
    existing = SimpleNamespace(installationId=77)
    session.exec.return_value.one_or_none.return_value = existing

    with _patch_github((12, 34)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 403
    assert existing.installationId == 77
    session.exec.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_installation_lookup_error_returns_502_without_persisting(
    client_and_session,
):
    client, session = client_and_session
    error = GithubException(500, {"message": "upstream failed"}, None)

    with _patch_github(installations_error=error):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Github error"}
    session.exec.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()
