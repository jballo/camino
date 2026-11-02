"""Follows that queue a new ingest share the /ingest rate-limit bucket (#92).

The Postgres counter is replaced by a test-only in-memory stand-in for
``consume_fixed_window``; the production limiter is unchanged.
"""

from collections import Counter
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_session
from app.main import app
from app.models.job import JobStatus
from app.rate_limit import RateLimitDecision
from app.security import get_authenticated_user_id
from app.services.repo_access import RepoAccess


USER_ID = "user_123"
RETRY_AFTER = 1800
LIMIT = settings.rate_limit_repository_ingest_requests


class FakeWindow:
    """Fixed-window counter with the real limit, minus the database."""

    def __init__(self):
        self.counts = Counter()

    def consume(self, *, bucket, user_id, request_limit, window_seconds):
        self.counts[(bucket, user_id)] += 1
        return RateLimitDecision(
            allowed=self.counts[(bucket, user_id)] <= request_limit,
            retry_after=RETRY_AFTER,
        )

    def used(self, user_id=USER_ID):
        return self.counts[("repository_ingest", user_id)]


@pytest.fixture
def window():
    return FakeWindow()


@pytest.fixture
def indexed():
    """Repository names the mocked session reports as already indexed."""
    return set()


@pytest.fixture(autouse=True)
def _app(window, indexed):
    def _session():
        session = MagicMock()
        session.exec.return_value.first.side_effect = lambda: (
            MagicMock() if current_repo["name"] in indexed else None
        )
        yield session

    current_repo = {"name": None}

    def _access(_session, _user_id, repo_name):
        current_repo["name"] = repo_name.lower()
        return RepoAccess(installation_id=12, visibility="public")

    app.dependency_overrides[get_authenticated_user_id] = lambda: USER_ID
    app.dependency_overrides[get_session] = _session
    job = MagicMock(id=1, status=JobStatus.PENDING)
    with (
        patch("app.rate_limit.consume_fixed_window", side_effect=window.consume),
        patch("app.api.repositories.resolve_repo_access", side_effect=_access),
        patch("app.api.repositories._installed_repository_names", return_value=set()),
        patch(
            "app.api.repositories.resolve_target_branch",
            return_value=SimpleNamespace(branch="main"),
        ),
        patch(
            "app.api.repositories.enqueue_shared_ingest",
            return_value=(job, job, True),
        ),
    ):
        yield
    app.dependency_overrides.clear()


client = TestClient(app)


def _follow(repo_name):
    return client.post("/api/v1/repositories/follows", json={"repoName": repo_name})


def test_follow_past_the_ingest_limit_gets_429_with_retry_after(window):
    for index in range(LIMIT):
        response = _follow(f"org/repo-{index}")
        assert response.status_code == 200
        assert response.json()["jobQueued"] is True

    response = _follow("org/one-too-many")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(RETRY_AFTER)
    assert window.used() == LIMIT + 1


def test_following_an_indexed_repository_never_consumes_the_window(window, indexed):
    indexed.add("org/indexed")

    for _ in range(LIMIT + 2):
        response = _follow("org/indexed")
        assert response.status_code == 200
        assert response.json()["jobQueued"] is False

    assert window.used() == 0
    assert _follow("org/new").status_code == 200
    assert window.used() == 1


def test_follows_and_ingest_requests_share_one_bucket(window):
    for index in range(LIMIT):
        assert _follow(f"org/repo-{index}").status_code == 200

    response = client.post(
        "/api/v1/repositories/ingest",
        json={"repoName": "org/other", "ref": "main"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(RETRY_AFTER)
