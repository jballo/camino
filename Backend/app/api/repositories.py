import asyncio
import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException
from github import Auth, GithubException, GithubIntegration
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc, text
from sqlmodel import Session, select

from app.config import settings
from app.services.embeddings import EmbeddingError
from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.models.code import RepoIndexState
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import (
    CONTRIBUTION_TARGET_RATE_LIMIT,
    REPOSITORY_INGEST_RATE_LIMIT,
    REPOSITORY_SEARCH_RATE_LIMIT,
)
from app.security import get_authenticated_user_id
from app.services.jobs import (
    cancel_job,
    enqueue_job,
    normalize_repository_name,
    repository_ingest_dedupe_key,
)
from app.services.search import hybrid_search
from app.services.repo_access import (
    RepoAccessDenied,
    RepoAccessUnavailable,
    authorize_index_read,
    resolve_repo_access,
)
from app.services.target_branch import resolve_target_branch

logger = logging.getLogger(__name__)

router = APIRouter()

class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    repoName: str
    ref: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class SearchResultResponse(BaseModel):
    chunk_id: int
    repo_name: str
    file_path: str
    symbol_name: str
    symbol_type: str
    language: str
    start_line: int
    end_line: int
    source_code: str
    signature: str
    docstring: str | None
    score: float



class RepoIngestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repoName: str
    ref: str | None = None


class RepoIngestJobResponse(BaseModel):
    id: int
    status: str


class RepoIngestStatusResponse(BaseModel):
    id: int
    status: str
    repoName: str
    ref: str | None
    attempts: int
    result: dict | None = None
    error: str | None = None


class ContributionTargetResponse(BaseModel):
    repoName: str
    targetBranch: str | None
    source: str
    evidence: str | None
    evidencePath: str | None
    defaultBranch: str | None
    checkedAt: dt.datetime


def _get_authorized_repository_ingest(
    session: Session,
    job_id: int,
    auth_user_id: str,
) -> Job:
    try:
        job = session.get(Job, job_id)
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if job is None or job.job_type != JobType.REPOSITORY_INGEST:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    if job.userId == auth_user_id:
        return job

    try:
        state = session.exec(
            select(RepoIndexState).where(
                RepoIndexState.repo_name == job.repo_name,
                RepoIndexState.ref == job.ref,
            )
        ).one_or_none()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    try:
        if state is not None:
            authorize_index_read(session, auth_user_id, state)
        else:
            resolve_repo_access(session, auth_user_id, job.repo_name)
    except RepoAccessDenied:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    except RepoAccessUnavailable:
        raise HTTPException(status_code=502, detail="Github access check failed")
    return job


def _resolve_readable_index(
    session: Session,
    user_id: str,
    repo_name: str,
    ref: str | None,
) -> RepoIndexState:
    statement = select(RepoIndexState).where(
        RepoIndexState.repo_name == normalize_repository_name(repo_name)
    )
    if ref is not None:
        statement = statement.where(RepoIndexState.ref == ref)
    try:
        states = session.exec(statement).all()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    if not states:
        raise HTTPException(status_code=404, detail="Repository index not found")
    if ref is None and len(states) != 1:
        raise HTTPException(
            status_code=409,
            detail="Repository has multiple indexed refs; specify ref",
        )
    state = states[0]
    try:
        authorize_index_read(session, user_id, state)
    except RepoAccessDenied:
        raise HTTPException(status_code=404, detail="Repository index not found")
    except RepoAccessUnavailable:
        raise HTTPException(status_code=502, detail="Github access check failed")
    return state


def _repository_ingest_response(job: Job) -> RepoIngestStatusResponse:
    return RepoIngestStatusResponse(
        id=job.id,
        status=job.status,
        repoName=job.repo_name,
        ref=job.ref,
        attempts=job.attempts,
        result=job.artifact,
        error=job.error,
    )


@router.get("")
async def list_repositories(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> list[str]:
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == auth_user_id
        )
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(
            app_id=settings.gh_app_id, private_key=settings.gh_app_private_key
        )
        gi = GithubIntegration(auth=app_auth)
        installation = gi.get_app_installation(gh_connection.installationId)
        repos = installation.get_repos()
        return [repo.full_name for repo in repos]
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")


@router.get("/processed")
async def list_processed_repositories(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> list[dict]:
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == auth_user_id
        )
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(
            app_id=settings.gh_app_id, private_key=settings.gh_app_private_key
        )
        installation = GithubIntegration(auth=app_auth).get_app_installation(
            gh_connection.installationId
        )
        installed_repos = {
            normalize_repository_name(repo.full_name)
            for repo in installation.get_repos()
        }
        rows = session.execute(
            text("""
                SELECT s.repo_name, s.ref, s.visibility, s.indexed_sha,
                       count(DISTINCT c.id) AS chunk_count,
                       bool_or(j.id IS NOT NULL) AS requested_by_user
                FROM repo_index_state AS s
                LEFT JOIN code_chunks AS c
                  ON c.repo_name = s.repo_name
                 AND c.ref = s.ref
                 AND c.generation = s.active_generation
                LEFT JOIN jobs AS j
                  ON j.repo_name = s.repo_name
                 AND j.ref = s.ref
                 AND j."userId" = :user_id
                GROUP BY s.repo_name, s.ref, s.visibility, s.indexed_sha
            """),
            {"user_id": auth_user_id},
        ).all()
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    return [
        {
            "repo_name": row.repo_name,
            "ref": row.ref,
            "visibility": row.visibility,
            "indexed_sha": row.indexed_sha,
            "chunk_count": row.chunk_count,
        }
        for row in rows
        if row.repo_name in installed_repos
        or (row.visibility == "public" and row.requested_by_user)
    ]


@router.get(
    "/contribution-target",
    dependencies=[Depends(CONTRIBUTION_TARGET_RATE_LIMIT)],
)
async def get_contribution_target(
    repoName: str,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> ContributionTargetResponse:
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == auth_user_id
        )
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    resolution = await asyncio.to_thread(
        resolve_target_branch,
        repoName,
        gh_connection.installationId,
    )
    return ContributionTargetResponse(
        repoName=repoName,
        targetBranch=resolution.branch,
        source=resolution.source,
        evidence=resolution.evidence,
        evidencePath=resolution.evidence_path,
        defaultBranch=resolution.default_branch,
        checkedAt=resolution.checked_at,
    )


@router.post("/ingest", dependencies=[Depends(REPOSITORY_INGEST_RATE_LIMIT)])
async def process_repository(
    payload: RepoIngestBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoIngestJobResponse:
    try:
        access = resolve_repo_access(session, auth_user_id, payload.repoName)
    except RepoAccessDenied as error:
        raise HTTPException(status_code=404, detail=str(error))
    except RepoAccessUnavailable as error:
        raise HTTPException(status_code=502, detail=str(error))
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    ref = payload.ref
    if ref is None:
        resolution = await asyncio.to_thread(
            resolve_target_branch,
            payload.repoName,
            access.installation_id,
        )
        ref = resolution.branch
    if not ref:
        raise HTTPException(status_code=422, detail="Could not resolve repository ref")

    try:
        job, created = enqueue_job(
            session,
            user_id=auth_user_id,
            installation_id=access.installation_id,
            repo_name=payload.repoName,
            ref=ref,
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key=repository_ingest_dedupe_key(
                repo_name=payload.repoName,
                ref=ref,
            ),
        )
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "repository ingest job %s | id=%s repo=%r",
        "queued" if created else "deduplicated",
        job.id,
        job.repo_name,
    )
    return RepoIngestJobResponse(id=job.id, status=job.status)


@router.get("/ingest/{job_id}")
async def get_repository_ingest(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoIngestStatusResponse:
    job = _get_authorized_repository_ingest(session, job_id, auth_user_id)
    return _repository_ingest_response(job)


@router.post("/ingest/{job_id}/cancel")
async def cancel_repository_ingest(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoIngestStatusResponse:
    job = _get_authorized_repository_ingest(session, job_id, auth_user_id)
    if job.userId != auth_user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the job owner can cancel this ingestion",
        )

    if job.status == JobStatus.CANCELLED:
        return _repository_ingest_response(job)
    if job.status not in JobStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel ingestion job with status '{job.status}'",
        )

    try:
        cancel_job(session, job_id)
        session.refresh(job)
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if job.status != JobStatus.CANCELLED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel ingestion job with status '{job.status}'",
        )
    return _repository_ingest_response(job)


@router.post("/search", dependencies=[Depends(REPOSITORY_SEARCH_RATE_LIMIT)])
async def search_repository(
    payload: SearchBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> list[SearchResultResponse]:
    try:
        index_state = _resolve_readable_index(
            session, auth_user_id, payload.repoName, payload.ref
        )
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        results = await hybrid_search(
            session,
            payload.query,
            payload.repoName,
            ref=index_state.ref,
            limit=payload.limit,
        )
    except EmbeddingError as e:
        logger.error(
            "Embedding service failed during search: %s | query=%r repo=%r",
            e, payload.query, payload.repoName,
        )
        raise HTTPException(status_code=502, detail="Embedding service unavailable")
    except exc.SQLAlchemyError as e:
        session.rollback()
        logger.error(
            "Database error during search: %s | query=%r repo=%r",
            e, payload.query, payload.repoName,
        )
        raise HTTPException(status_code=502, detail="Database service error")
    except Exception as e:
        logger.exception(
            "Unexpected error during search | query=%r repo=%r",
            payload.query, payload.repoName,
        )
        raise HTTPException(status_code=500, detail="Internal search error")

    if not results:
        return []
    return [
        SearchResultResponse(
            chunk_id=r.chunk_id,
            repo_name=r.repo_name,
            file_path=r.file_path,
            symbol_name=r.symbol_name,
            symbol_type=r.symbol_type,
            language=r.language,
            start_line=r.start_line,
            end_line=r.end_line,
            source_code=r.source_code,
            signature=r.signature,
            docstring=r.docstring,
            score=r.score,
        )
        for r in results
    ]
