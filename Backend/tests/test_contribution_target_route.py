import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import exc

from app.db import get_session
from app.main import app
from app.rate_limit import CONTRIBUTION_TARGET_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.repo_access import (
    RepoAccess,
    RepoAccessDenied,
    RepoAccessUnavailable,
)
from app.services.target_branch import TargetBranchResolution

USER_ID = "user_123"
INSTALLATION_ID = 456
URL = "/api/v1/repositories/contribution-target"


@pytest.fixture(autouse=True)
def session():
    mock_session = MagicMock()

    def _session():
        yield mock_session

    app.dependency_overrides[get_authenticated_user_id] = lambda: USER_ID
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[CONTRIBUTION_TARGET_RATE_LIMIT] = lambda: None
    yield mock_session
    app.dependency_overrides.clear()


client = TestClient(app)


def test_get_returns_resolved_contribution_target(session):
    checked_at = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC)
    resolution = TargetBranchResolution(
        branch="develop",
        source="contributing_doc",
        evidence="Please target the `develop` branch.",
        evidence_path="CONTRIBUTING.md",
        default_branch="main",
        checked_at=checked_at,
    )
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(
                installation_id=INSTALLATION_ID,
                visibility="public",
            ),
        ) as resolve_access,
        patch(
            "app.api.repositories.resolve_target_branch",
            return_value=resolution,
        ) as resolve,
    ):
        response = client.get(URL, params={"repoName": "Org/Repo"})

    assert response.status_code == 200
    assert response.json() == {
        "repoName": "Org/Repo",
        "targetBranch": "develop",
        "source": "contributing_doc",
        "evidence": "Please target the `develop` branch.",
        "evidencePath": "CONTRIBUTING.md",
        "defaultBranch": "main",
        "checkedAt": "2026-09-13T12:00:00Z",
    }
    resolve_access.assert_called_once_with(session, USER_ID, "Org/Repo")
    resolve.assert_called_once_with("Org/Repo", INSTALLATION_ID)


def test_get_returns_404_for_private_repository():
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            side_effect=RepoAccessDenied(
                "Private repositories are not supported"
            ),
        ),
        patch("app.api.repositories.resolve_target_branch") as resolve,
    ):
        response = client.get(URL, params={"repoName": "org/private-repo"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Repository not found"}
    resolve.assert_not_called()


def test_get_returns_404_without_github_connection():
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            side_effect=RepoAccessDenied(
                "GitHub connection not found for user"
            ),
        ),
        patch("app.api.repositories.resolve_target_branch") as resolve,
    ):
        response = client.get(URL, params={"repoName": "org/repo"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Repository not found"}
    resolve.assert_not_called()


def test_get_returns_502_when_access_check_unavailable():
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            side_effect=RepoAccessUnavailable("GitHub unreachable"),
        ),
        patch("app.api.repositories.resolve_target_branch") as resolve,
    ):
        response = client.get(URL, params={"repoName": "org/repo"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Github access check failed"}
    resolve.assert_not_called()


def test_get_returns_500_on_database_error(session):
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            side_effect=exc.SQLAlchemyError(),
        ),
        patch("app.api.repositories.resolve_target_branch") as resolve,
    ):
        response = client.get(URL, params={"repoName": "org/repo"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Database error"}
    session.rollback.assert_called_once()
    resolve.assert_not_called()


def test_get_returns_unresolved_result_as_200():
    resolution = TargetBranchResolution(
        branch=None,
        source="unresolved",
        evidence=None,
        evidence_path=None,
        default_branch=None,
        checked_at=dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC),
    )
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(
                installation_id=INSTALLATION_ID,
                visibility="public",
            ),
        ),
        patch(
            "app.api.repositories.resolve_target_branch",
            return_value=resolution,
        ),
    ):
        response = client.get(URL, params={"repoName": "org/repo"})

    assert response.status_code == 200
    assert response.json()["targetBranch"] is None
    assert response.json()["source"] == "unresolved"


def test_route_wires_contribution_target_rate_limit():
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == URL
    )

    assert CONTRIBUTION_TARGET_RATE_LIMIT in [
        dependency.call for dependency in route.dependant.dependencies
    ]
