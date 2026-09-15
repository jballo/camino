import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.briefs import (
    BriefRequest,
    BriefPreviewResponse,
    ForkStatusPreview,
    TargetBranchPreview,
    _preview,
    parse_issue_url,
)
from app.db import get_session
from app.main import app
from app.models.job import JobStatus, JobType
from app.rate_limit import ISSUE_BRIEF_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.fork_status import ForkStatus
from app.services.issue_thread import IssueThread
from app.services.repo_access import RepoAccess
from app.services.target_branch import TargetBranchResolution


def authenticated():
    return "user_123"


def session_override():
    yield MagicMock()


@pytest.fixture(autouse=True)
def overrides():
    app.dependency_overrides[get_authenticated_user_id] = authenticated
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[ISSUE_BRIEF_CREATE_RATE_LIMIT] = lambda: None
    yield
    app.dependency_overrides.clear()


client = TestClient(app)


@pytest.mark.parametrize("url", [
    "http://github.com/org/repo/issues/44",
    "https://example.com/org/repo/issues/44",
    "https://github.com/org/repo/pull/44",
    "https://github.com/org/repo/issues/0",
])
def test_issue_url_parser_rejects_non_issue_urls(url):
    with pytest.raises(ValueError):
        parse_issue_url(url)


def test_issue_url_parser_normalizes_repository():
    assert parse_issue_url("https://github.com/Org/Repo/issues/44") == ("org/repo", 44)


def preview() -> BriefPreviewResponse:
    return BriefPreviewResponse(
        issueUrl="https://github.com/org/repo/issues/44",
        issueRepo="org/repo",
        repoName="org/repo",
        issueNumber=44,
        title="Fix refresh races",
        state="open",
        labels=["bug"],
        assignees=[],
        warnings=[],
        targetBranch=TargetBranchPreview(
            branch="develop",
            source="contributing_doc",
            evidence="Target develop",
            evidencePath="CONTRIBUTING.md",
            defaultBranch="main",
        ),
        forkStatus=ForkStatusPreview(
            forkRepo=None,
            upstreamRepo="org/repo",
            commitsBehind=0,
            measurable=True,
        ),
    )


def test_preview_returns_metadata_and_warnings():
    with patch(
        "app.api.briefs._preview",
        new_callable=AsyncMock,
        return_value=(preview(), 123),
    ):
        response = client.post(
            "/api/v1/briefs/preview",
            json={"issueUrl": "https://github.com/org/repo/issues/44"},
        )
    assert response.status_code == 200
    assert response.json()["targetBranch"]["branch"] == "develop"


async def test_preview_keeps_fork_issue_identity_separate_from_upstream_code():
    issue = IssueThread(
        repo_name="contributor/repo",
        number=44,
        title="Fork-only issue",
        body="",
        state="open",
        labels=(),
        assignees=(),
        comments=(),
        html_url="https://github.com/contributor/repo/issues/44",
        warnings=(),
        branch_instruction=None,
    )
    initial_fork = ForkStatus(
        requested_repo="contributor/repo",
        upstream_repo="org/repo",
        fork_repo="contributor/repo",
        is_fork=True,
        target_branch=None,
        commits_behind=None,
        measurable=False,
    )
    measured_fork = ForkStatus(
        requested_repo="contributor/repo",
        upstream_repo="org/repo",
        fork_repo="contributor/repo",
        is_fork=True,
        target_branch="main",
        commits_behind=2,
        measurable=True,
    )
    target = TargetBranchResolution(
        branch="main",
        source="repository_default",
        evidence="repository default branch",
        evidence_path=None,
        default_branch="main",
        checked_at=dt.datetime.now(dt.UTC),
    )

    with (
        patch(
            "app.api.briefs.resolve_repo_access",
            return_value=RepoAccess(installation_id=123, visibility="public"),
        ),
        patch("app.api.briefs.fetch_issue_thread", return_value=issue),
        patch(
            "app.api.briefs.resolve_fork_status",
            side_effect=[initial_fork, measured_fork],
        ),
        patch("app.api.briefs.resolve_target_branch", return_value=target),
    ):
        result, installation_id = await _preview(
            BriefRequest(
                issueUrl="https://github.com/contributor/repo/issues/44"
            ),
            MagicMock(),
            "user_123",
        )

    assert installation_id == 123
    assert result.issueRepo == "contributor/repo"
    assert result.repoName == "org/repo"


def test_cold_start_wires_brief_to_ingestion_dependency():
    session = MagicMock()
    session.exec.return_value.one_or_none.return_value = None

    def custom_session():
        yield session

    app.dependency_overrides[get_session] = custom_session
    ingest = MagicMock(id=7, status=JobStatus.PENDING)
    brief_job = MagicMock(id=8, status=JobStatus.PENDING)
    fork_preview = preview().model_copy(
        update={
            "issueUrl": "https://github.com/contributor/repo/issues/44",
            "issueRepo": "contributor/repo",
        }
    )
    with (
        patch(
            "app.api.briefs._preview",
            new_callable=AsyncMock,
            return_value=(fork_preview, 123),
        ),
        patch(
            "app.api.briefs.enqueue_job",
            side_effect=[(ingest, True), (brief_job, True)],
        ) as enqueue,
    ):
        response = client.post(
            "/api/v1/briefs",
            json={
                "issueUrl": "https://github.com/contributor/repo/issues/44",
                "targetBranch": "develop",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"id": 8, "status": "pending"}
    assert enqueue.call_args_list[0].kwargs["job_type"] == JobType.REPOSITORY_INGEST
    assert enqueue.call_args_list[0].kwargs["repo_name"] == "org/repo"
    assert enqueue.call_args_list[1].kwargs["job_type"] == JobType.ISSUE_BRIEF
    assert enqueue.call_args_list[1].kwargs["repo_name"] == "org/repo"
    assert enqueue.call_args_list[1].kwargs["issue_repo"] == "contributor/repo"
    assert enqueue.call_args_list[1].kwargs["blocked_by_job_id"] == 7


def test_get_brief_is_user_scoped_and_reports_refresh_phase():
    job = MagicMock(
        id=8,
        status=JobStatus.PENDING,
        job_type=JobType.ISSUE_BRIEF,
        userId="user_123",
        repo_name="org/repo",
        issue_repo="org/repo",
        ref="develop",
        issue_number=44,
        topic="Fix refresh races",
        artifact=None,
        error=None,
        blocked_by_job_id=7,
        createdAt=dt.datetime.now(dt.UTC),
    )
    dependency = MagicMock(id=7, status=JobStatus.RUNNING)
    session = MagicMock()
    session.get.side_effect = [job, dependency]

    def custom_session():
        yield session

    app.dependency_overrides[get_session] = custom_session
    response = client.get("/api/v1/briefs/8")
    assert response.status_code == 200
    assert response.json()["phase"] == "blocked_on_ingest"


def test_get_brief_forbids_another_user():
    job = MagicMock(job_type=JobType.ISSUE_BRIEF, userId="other")
    session = MagicMock()
    session.get.return_value = job

    def custom_session():
        yield session

    app.dependency_overrides[get_session] = custom_session
    assert client.get("/api/v1/briefs/8").status_code == 403
