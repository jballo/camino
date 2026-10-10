"""Briefs that start a new ingest share the /ingest rate-limit bucket (#100).

The Postgres counter is replaced by a test-only in-memory stand-in for
``consume_fixed_window``; the production limiter is unchanged. The issue-brief
bucket is overridden so these tests only see the ingest bucket.
"""

from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.briefs import (
    BriefPreviewResponse,
    ForkStatusPreview,
    TargetBranchPreview,
)
from app.config import settings
from app.db import get_session
from app.main import app
from app.models.job import JobStatus
from app.rate_limit import (
    ISSUE_BRIEF_CREATE_RATE_LIMIT,
    REPOSITORY_INGEST_RATE_LIMIT_DETAIL,
    RateLimitDecision,
)
from app.security import get_authenticated_user_id


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


def _preview(repo_name: str) -> BriefPreviewResponse:
    return BriefPreviewResponse(
        issueUrl=f"https://github.com/{repo_name}/issues/44",
        repoName=repo_name,
        issueRepo=repo_name,
        issueNumber=44,
        title="Fix the thing",
        state="open",
        labels=[],
        assignees=[],
        targetBranch=TargetBranchPreview(
            branch="main",
            source="default_branch",
            evidence=None,
            evidencePath=None,
            defaultBranch="main",
        ),
        forkStatus=ForkStatusPreview(
            forkRepo=None,
            upstreamRepo=repo_name,
            commitsBehind=None,
            measurable=False,
        ),
        warnings=[],
    )


@pytest.fixture
def window():
    return FakeWindow()


@pytest.fixture
def indexed():
    """Repository names the mocked session reports as already indexed."""
    return set()


@pytest.fixture
def active_ingests():
    """Repository names whose shared ingest someone else already started."""
    return set()


@pytest.fixture
def session():
    return MagicMock()


@pytest.fixture(autouse=True)
def _app(window, indexed, active_ingests, session):
    current_repo = {"name": None}

    async def preview(payload, _session, _user_id):
        repo_name = payload.issueUrl.split("github.com/")[1].split("/issues")[0]
        current_repo["name"] = repo_name
        return _preview(repo_name), 12

    session.exec.return_value.one_or_none.side_effect = lambda: (
        MagicMock() if current_repo["name"] in indexed else None
    )

    def enqueue_shared_ingest(_session, *, repo_name, **_kwargs):
        ingest = MagicMock(id=7, status=JobStatus.PENDING)
        return ingest, ingest, repo_name not in active_ingests

    def _session():
        yield session

    app.dependency_overrides[get_authenticated_user_id] = lambda: USER_ID
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[ISSUE_BRIEF_CREATE_RATE_LIMIT] = lambda: None
    brief = MagicMock(id=8, status=JobStatus.PENDING)
    with (
        patch("app.rate_limit.consume_fixed_window", side_effect=window.consume),
        patch("app.api.briefs._preview", new=AsyncMock(side_effect=preview)),
        patch("app.api.briefs.authorize_index_read"),
        patch(
            "app.api.briefs.enqueue_shared_ingest",
            side_effect=enqueue_shared_ingest,
        ),
        patch("app.api.briefs.enqueue_job", return_value=(brief, True)),
    ):
        yield
    app.dependency_overrides.clear()


client = TestClient(app)


def _brief(repo_name):
    return client.post(
        "/api/v1/briefs",
        json={"issueUrl": f"https://github.com/{repo_name}/issues/44"},
    )


def test_brief_that_starts_an_ingest_consumes_the_ingest_bucket_once(window):
    response = _brief("org/new")

    assert response.status_code == 200
    assert window.used() == 1


def test_brief_for_an_indexed_repository_never_consumes_the_window(
    window, indexed
):
    indexed.add("org/indexed")

    for _ in range(LIMIT + 2):
        assert _brief("org/indexed").status_code == 200

    assert window.used() == 0


def test_brief_joining_an_active_ingest_never_consumes_the_window(
    window, active_ingests
):
    active_ingests.add("org/busy")

    for _ in range(LIMIT + 2):
        assert _brief("org/busy").status_code == 200

    assert window.used() == 0


def test_brief_past_the_ingest_limit_gets_429_and_saves_nothing(window, session):
    for index in range(LIMIT):
        assert _brief(f"org/repo-{index}").status_code == 200
    session.reset_mock()

    response = _brief("org/one-too-many")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(RETRY_AFTER)
    assert response.json()["detail"] == REPOSITORY_INGEST_RATE_LIMIT_DETAIL
    session.commit.assert_not_called()
    session.rollback.assert_called()


def test_brief_fails_closed_when_the_limiter_is_unavailable(session):
    with patch(
        "app.rate_limit.consume_fixed_window",
        side_effect=HTTPException(
            status_code=503, detail="Rate limit service unavailable"
        ),
    ):
        response = _brief("org/new")

    assert response.status_code == 503
    session.commit.assert_not_called()
    session.rollback.assert_called()


def test_briefs_and_ingest_requests_share_one_bucket(window):
    for index in range(LIMIT):
        assert _brief(f"org/repo-{index}").status_code == 200

    response = client.post(
        "/api/v1/repositories/ingest",
        json={"repoName": "org/other", "ref": "main"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(RETRY_AFTER)
