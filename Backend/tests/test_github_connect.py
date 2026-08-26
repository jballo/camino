import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from psycopg2.errorcodes import NOT_NULL_VIOLATION, UNIQUE_VIOLATION
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


def _patch_github():
    oauth_app = MagicMock()
    oauth_app.get_access_token.return_value = _access_token()

    github_user = MagicMock()
    github_user.login = "octocat"
    github_user.id = GITHUB_USER_ID

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


@patch("app.api.github.encrypt_token", side_effect=lambda value: f"enc-{value}")
def test_connect_persists_github_user_id(_encrypt, client_and_session):
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


@patch("app.api.github.encrypt_token", side_effect=lambda value: f"enc-{value}")
def test_connect_updates_github_user_id_on_existing_row(_encrypt, client_and_session):
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


@patch("app.api.github.encrypt_token", side_effect=lambda value: f"enc-{value}")
def test_unique_user_conflict_returns_409(_encrypt, client_and_session):
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


@patch("app.api.github.encrypt_token", side_effect=lambda value: f"enc-{value}")
def test_not_null_integrity_error_returns_500(_encrypt, client_and_session):
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
