"""Issue-brief preview, queue, reader, and cancellation endpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc
from sqlmodel import Session, select

from app.db import SessionDep
from app.models.brief import BriefWarning
from app.models.code import RepoIndexState
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import ISSUE_BRIEF_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.fork_status import ForkStatus, resolve_fork_status
from app.services.issue_thread import IssueThread, IssueThreadError, fetch_issue_thread
from app.services.jobs import (
    cancel_job,
    enqueue_job,
    issue_brief_dedupe_key,
    normalize_repository_name,
    repository_ingest_dedupe_key,
)
from app.services.repo_access import (
    RepoAccessDenied,
    RepoAccessUnavailable,
    authorize_index_read,
    resolve_repo_access,
)
from app.services.target_branch import TargetBranchResolution, resolve_target_branch

router = APIRouter()
_ISSUE_PATH = re.compile(r"^/([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?$")


class BriefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issueUrl: str = Field(min_length=1, max_length=2000)
    targetBranch: str | None = Field(default=None, min_length=1, max_length=255)


class TargetBranchPreview(BaseModel):
    branch: str | None
    source: str
    evidence: str | None
    evidencePath: str | None
    defaultBranch: str | None


class ForkStatusPreview(BaseModel):
    forkRepo: str | None
    upstreamRepo: str
    commitsBehind: int | None
    measurable: bool


class BriefPreviewResponse(BaseModel):
    issueUrl: str
    issueRepo: str
    repoName: str
    issueNumber: int
    title: str
    state: str
    labels: list[str]
    assignees: list[str]
    warnings: list[BriefWarning]
    targetBranch: TargetBranchPreview
    forkStatus: ForkStatusPreview


class BriefCreatedResponse(BaseModel):
    id: int
    status: str


class BriefResponse(BaseModel):
    id: int
    status: str
    phase: str
    repoName: str
    issueRepo: str
    ref: str | None
    issueNumber: int
    issueTitle: str
    artifact: dict | None = None
    error: str | None = None


class BriefSummaryResponse(BaseModel):
    id: int
    status: str
    repoName: str
    issueRepo: str
    ref: str | None
    issueNumber: int
    issueTitle: str
    createdAt: dt.datetime


def parse_issue_url(value: str) -> tuple[str, int]:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("Enter a full GitHub issue URL")
    match = _ISSUE_PATH.fullmatch(unquote(parsed.path))
    if match is None:
        raise ValueError("Enter a URL like https://github.com/owner/repo/issues/123")
    owner, repo, number = match.groups()
    return normalize_repository_name(f"{owner}/{repo}"), int(number)


def _issue_resolution(
    issue: IssueThread,
    resolved: TargetBranchResolution,
    override: str | None,
) -> TargetBranchResolution:
    if override:
        return TargetBranchResolution(
            branch=override,
            source="user_override",
            evidence="Target branch selected by the requester",
            evidence_path=None,
            default_branch=resolved.default_branch,
            checked_at=dt.datetime.now(dt.UTC),
        )
    if issue.branch_instruction is not None:
        return TargetBranchResolution(
            branch=issue.branch_instruction.branch,
            source="issue_thread",
            evidence=issue.branch_instruction.evidence,
            evidence_path=None,
            default_branch=resolved.default_branch,
            checked_at=dt.datetime.now(dt.UTC),
        )
    return resolved


async def _preview(
    payload: BriefRequest,
    session: Session,
    user_id: str,
) -> tuple[BriefPreviewResponse, int]:
    try:
        requested_repo, issue_number = parse_issue_url(payload.issueUrl)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    try:
        access = resolve_repo_access(session, user_id, requested_repo)
    except RepoAccessDenied as error:
        raise HTTPException(status_code=404, detail=str(error))
    except RepoAccessUnavailable as error:
        raise HTTPException(status_code=502, detail=str(error))
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        issue, initial_fork = await asyncio.gather(
            asyncio.to_thread(
                fetch_issue_thread, requested_repo, issue_number, access.installation_id
            ),
            asyncio.to_thread(
                resolve_fork_status, requested_repo, access.installation_id, None
            ),
        )
        resolved = await asyncio.to_thread(
            resolve_target_branch, initial_fork.upstream_repo, access.installation_id
        )
        target = _issue_resolution(issue, resolved, payload.targetBranch)
        fork = await asyncio.to_thread(
            resolve_fork_status,
            requested_repo,
            access.installation_id,
            target.branch,
        )
    except IssueThreadError as error:
        raise HTTPException(status_code=404, detail=str(error))

    return BriefPreviewResponse(
        issueUrl=payload.issueUrl,
        issueRepo=requested_repo,
        repoName=fork.upstream_repo,
        issueNumber=issue.number,
        title=issue.title,
        state=issue.state,
        labels=list(issue.labels),
        assignees=list(issue.assignees),
        warnings=list(issue.warnings),
        targetBranch=TargetBranchPreview(
            branch=target.branch,
            source=target.source,
            evidence=target.evidence,
            evidencePath=target.evidence_path,
            defaultBranch=target.default_branch,
        ),
        forkStatus=ForkStatusPreview(
            forkRepo=fork.fork_repo,
            upstreamRepo=fork.upstream_repo,
            commitsBehind=fork.commits_behind,
            measurable=fork.measurable,
        ),
    ), access.installation_id


@router.post("/preview", dependencies=[Depends(ISSUE_BRIEF_CREATE_RATE_LIMIT)])
async def preview_brief(
    payload: BriefRequest,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> BriefPreviewResponse:
    preview, _ = await _preview(payload, session, auth_user_id)
    return preview


@router.post("", dependencies=[Depends(ISSUE_BRIEF_CREATE_RATE_LIMIT)])
async def create_brief(
    payload: BriefRequest,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> BriefCreatedResponse:
    preview, installation_id = await _preview(payload, session, auth_user_id)
    ref = preview.targetBranch.branch
    if not ref:
        raise HTTPException(status_code=422, detail="Could not resolve a target branch")
    repo_name = normalize_repository_name(preview.repoName)
    try:
        state = session.exec(
            select(RepoIndexState).where(
                RepoIndexState.repo_name == repo_name,
                RepoIndexState.ref == ref,
            )
        ).one_or_none()
        dependency_id = None
        if state is not None:
            authorize_index_read(session, auth_user_id, state)
        else:
            ingest, _ = enqueue_job(
                session,
                user_id=auth_user_id,
                installation_id=installation_id,
                repo_name=repo_name,
                ref=ref,
                job_type=JobType.REPOSITORY_INGEST,
                dedupe_key=repository_ingest_dedupe_key(repo_name=repo_name, ref=ref),
            )
            dependency_id = ingest.id
        brief, _ = enqueue_job(
            session,
            user_id=auth_user_id,
            installation_id=installation_id,
            repo_name=repo_name,
            ref=ref,
            job_type=JobType.ISSUE_BRIEF,
            dedupe_key=issue_brief_dedupe_key(
                user_id=auth_user_id,
                repo_name=repo_name,
                ref=ref,
                issue_repo=preview.issueRepo,
                issue_number=preview.issueNumber,
            ),
            topic=preview.title,
            issue_repo=preview.issueRepo,
            issue_number=preview.issueNumber,
            blocked_by_job_id=dependency_id,
        )
    except RepoAccessDenied:
        raise HTTPException(status_code=404, detail="Repository index not found")
    except RepoAccessUnavailable:
        raise HTTPException(status_code=502, detail="Github access check failed")
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    return BriefCreatedResponse(id=brief.id, status=brief.status)


def _authorized_brief(session: Session, job_id: int, user_id: str) -> Job:
    try:
        job = session.get(Job, job_id)
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    if job is None or job.job_type != JobType.ISSUE_BRIEF:
        raise HTTPException(status_code=404, detail="Brief not found")
    if job.userId != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return job


def _phase(session: Session, job: Job) -> str:
    if job.status == JobStatus.PENDING and job.blocked_by_job_id is not None:
        dependency = session.get(Job, job.blocked_by_job_id)
        if dependency is not None and dependency.status in JobStatus.ACTIVE:
            return "blocked_on_ingest"
    if job.status == JobStatus.PENDING:
        return "queued"
    if job.status == JobStatus.RUNNING:
        return "generating"
    return job.status


def _response(session: Session, job: Job) -> BriefResponse:
    return BriefResponse(
        id=job.id,
        status=job.status,
        phase=_phase(session, job),
        repoName=job.repo_name,
        issueRepo=job.issue_repo or job.repo_name,
        ref=job.ref,
        issueNumber=job.issue_number,
        issueTitle=job.topic or f"Issue #{job.issue_number}",
        artifact=job.artifact,
        error=job.error,
    )


@router.get("")
async def list_briefs(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> list[BriefSummaryResponse]:
    jobs = session.exec(
        select(Job).where(
            Job.userId == auth_user_id,
            Job.job_type == JobType.ISSUE_BRIEF,
        ).order_by(Job.createdAt.desc())
    ).all()
    return [
        BriefSummaryResponse(
            id=job.id,
            status=job.status,
            repoName=job.repo_name,
            issueRepo=job.issue_repo or job.repo_name,
            ref=job.ref,
            issueNumber=job.issue_number,
            issueTitle=job.topic or f"Issue #{job.issue_number}",
            createdAt=job.createdAt,
        )
        for job in jobs
    ]


@router.get("/{job_id}")
async def get_brief(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> BriefResponse:
    return _response(session, _authorized_brief(session, job_id, auth_user_id))


@router.post("/{job_id}/cancel")
async def cancel_brief(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> BriefResponse:
    job = _authorized_brief(session, job_id, auth_user_id)
    if job.status == JobStatus.CANCELLED:
        return _response(session, job)
    if job.status not in JobStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel brief with status '{job.status}'",
        )
    cancel_job(session, job_id)
    session.refresh(job)
    return _response(session, job)
