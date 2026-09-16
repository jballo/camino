import asyncio
import datetime as dt
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from github import Auth, GithubException, GithubIntegration
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.config import settings
from app.services.embeddings import EmbeddingError
from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.models.code import RepoIndexState
from app.models.job import Job, JobStatus, JobType
from app.models.repo_follow import UserRepoFollow
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


class RepoRefResponse(BaseModel):
    ref: str
    chunkCount: int
    indexedSha: str | None
    indexedAt: dt.datetime | None


class RepoLookupResponse(BaseModel):
    repoName: str
    visibility: str
    indexed: bool
    followed: bool
    refs: list[RepoRefResponse]


class RepoEntryResponse(BaseModel):
    repoName: str
    refs: list[RepoRefResponse]


class RepoOverviewResponse(BaseModel):
    installed: list[RepoEntryResponse]
    requested: list[RepoEntryResponse]


class RepoFollowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repoName: str


class RepoFollowResponse(BaseModel):
    repoName: str
    followed: bool
    indexed: bool
    jobQueued: bool


_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _normalized_repository_name(repo_name: str) -> str:
    candidate = repo_name.strip()
    if not _REPOSITORY_NAME.fullmatch(candidate):
        raise HTTPException(
            status_code=422,
            detail="Repository name must use the owner/repo format",
        )
    return normalize_repository_name(candidate)


def _installed_repositories(installation_id: int) -> list[str]:
    app_auth = Auth.AppAuth(
        app_id=settings.gh_app_id,
        private_key=settings.gh_app_private_key,
    )
    installation = GithubIntegration(auth=app_auth).get_app_installation(
        installation_id
    )
    return [repo.full_name for repo in installation.get_repos()]


def _installed_repository_names(installation_id: int) -> set[str]:
    return {
        normalize_repository_name(repo_name)
        for repo_name in _installed_repositories(installation_id)
    }


def _index_rows(session: Session, repo_name: str | None = None) -> list:
    where = "WHERE s.repo_name = :repo_name" if repo_name is not None else ""
    return session.execute(
        text(f"""
            SELECT s.repo_name, s.ref, s.indexed_sha, s.indexed_at,
                   count(DISTINCT c.id) AS chunk_count
            FROM repo_index_state AS s
            LEFT JOIN code_chunks AS c
              ON c.repo_name = s.repo_name
             AND c.ref = s.ref
             AND c.generation = s.active_generation
            {where}
            GROUP BY s.repo_name, s.ref, s.indexed_sha, s.indexed_at
            ORDER BY s.repo_name, s.ref
        """),
        {} if repo_name is None else {"repo_name": repo_name},
    ).all()


def _ref_response(row) -> RepoRefResponse:
    return RepoRefResponse(
        ref=row.ref,
        chunkCount=row.chunk_count,
        indexedSha=row.indexed_sha,
        indexedAt=row.indexed_at,
    )


async def _get_authorized_repository_ingest(
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
            await asyncio.to_thread(
                authorize_index_read,
                session,
                auth_user_id,
                state,
            )
        else:
            await asyncio.to_thread(
                resolve_repo_access,
                session,
                auth_user_id,
                job.repo_name,
            )
    except RepoAccessDenied:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    except RepoAccessUnavailable:
        raise HTTPException(status_code=502, detail="Github access check failed")
    return job


async def _resolve_readable_index(
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
        await asyncio.to_thread(authorize_index_read, session, user_id, state)
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
        return await asyncio.to_thread(
            _installed_repositories,
            gh_connection.installationId,
        )
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")


@router.get(
    "/lookup",
    response_model=RepoLookupResponse,
    dependencies=[Depends(REPOSITORY_SEARCH_RATE_LIMIT)],
)
async def lookup_repository(
    repoName: str,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoLookupResponse:
    repo_name = _normalized_repository_name(repoName)
    try:
        access = await asyncio.to_thread(
            resolve_repo_access,
            session,
            auth_user_id,
            repo_name,
        )
        rows = _index_rows(session, repo_name)
        followed = session.exec(
            select(UserRepoFollow).where(
                UserRepoFollow.userId == auth_user_id,
                UserRepoFollow.repo_name == repo_name,
            )
        ).first() is not None
    except RepoAccessDenied:
        raise HTTPException(status_code=404, detail="Repository not found")
    except RepoAccessUnavailable:
        raise HTTPException(status_code=502, detail="Github access check failed")
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    return RepoLookupResponse(
        repoName=repo_name,
        visibility=access.visibility,
        indexed=bool(rows),
        followed=followed,
        refs=[_ref_response(row) for row in rows],
    )


@router.get("/overview", response_model=RepoOverviewResponse)
async def repository_overview(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoOverviewResponse:
    try:
        connection = session.exec(
            select(GithubConnections).where(
                GithubConnections.userId == auth_user_id
            )
        ).one()
        installed_names = await asyncio.to_thread(
            _installed_repository_names,
            connection.installationId,
        )
        followed_names = {
            row.repo_name
            for row in session.exec(
                select(UserRepoFollow).where(
                    UserRepoFollow.userId == auth_user_id
                )
            ).all()
        }
        authorized_requested_names: set[str] = set()
        for repo_name in followed_names - installed_names:
            try:
                await asyncio.to_thread(
                    resolve_repo_access,
                    session,
                    auth_user_id,
                    repo_name,
                )
            except RepoAccessDenied:
                logger.info(
                    "Omitting inaccessible followed repository from overview",
                    extra={"repo_name": repo_name, "user_id": auth_user_id},
                )
                continue
            except RepoAccessUnavailable:
                raise HTTPException(
                    status_code=502,
                    detail="Github access check failed",
                )
            authorized_requested_names.add(repo_name)

        visible_names = installed_names | authorized_requested_names
        rows = _index_rows(session)
    except exc.NoResultFound:
        raise HTTPException(
            status_code=404,
            detail="Github connection not found for user",
        )
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    refs_by_repo: dict[str, list[RepoRefResponse]] = {}
    for row in rows:
        if row.repo_name in visible_names:
            refs_by_repo.setdefault(row.repo_name, []).append(_ref_response(row))

    def entry(repo_name: str) -> RepoEntryResponse:
        return RepoEntryResponse(
            repoName=repo_name,
            refs=refs_by_repo.get(repo_name, []),
        )

    return RepoOverviewResponse(
        installed=[entry(name) for name in sorted(installed_names)],
        requested=[
            entry(name)
            for name in sorted(authorized_requested_names)
        ],
    )


@router.post("/follows", response_model=RepoFollowResponse)
async def follow_repository(
    payload: RepoFollowBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoFollowResponse:
    repo_name = _normalized_repository_name(payload.repoName)
    try:
        access = await asyncio.to_thread(
            resolve_repo_access,
            session,
            auth_user_id,
            repo_name,
        )
        installed_names = await asyncio.to_thread(
            _installed_repository_names,
            access.installation_id,
        )
        installed = repo_name in installed_names
        indexed = session.exec(
            select(RepoIndexState).where(RepoIndexState.repo_name == repo_name)
        ).first() is not None
    except RepoAccessDenied:
        raise HTTPException(status_code=404, detail="Repository not found")
    except RepoAccessUnavailable:
        raise HTTPException(status_code=502, detail="Github access check failed")
    except GithubException:
        raise HTTPException(status_code=502, detail="Github access check failed")
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    resolution = None
    if not indexed:
        resolution = await asyncio.to_thread(
            resolve_target_branch,
            repo_name,
            access.installation_id,
        )
        if not resolution.branch:
            raise HTTPException(
                status_code=422,
                detail="Could not resolve repository ref",
            )

    followed = False
    job_queued = False
    try:
        if not installed:
            session.execute(
                pg_insert(UserRepoFollow)
                .values(userId=auth_user_id, repo_name=repo_name)
                .on_conflict_do_nothing(constraint="uq_user_repo_follow")
            )
            followed = True

        if resolution is not None:
            _, job_queued = enqueue_job(
                session,
                user_id=auth_user_id,
                installation_id=access.installation_id,
                repo_name=repo_name,
                ref=resolution.branch,
                job_type=JobType.REPOSITORY_INGEST,
                dedupe_key=repository_ingest_dedupe_key(
                    repo_name=repo_name,
                    ref=resolution.branch,
                ),
                commit=False,
            )
        session.commit()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    return RepoFollowResponse(
        repoName=repo_name,
        followed=followed,
        indexed=indexed,
        jobQueued=job_queued,
    )


@router.delete("/follows/{owner}/{repo}", status_code=204)
async def unfollow_repository(
    owner: str,
    repo: str,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> None:
    repo_name = _normalized_repository_name(f"{owner}/{repo}")
    try:
        follow = session.exec(
            select(UserRepoFollow).where(
                UserRepoFollow.userId == auth_user_id,
                UserRepoFollow.repo_name == repo_name,
            )
        ).first()
        if follow is not None:
            session.delete(follow)
            session.commit()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")


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
        access = await asyncio.to_thread(
            resolve_repo_access,
            session,
            auth_user_id,
            payload.repoName,
        )
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
    job = await _get_authorized_repository_ingest(
        session,
        job_id,
        auth_user_id,
    )
    return _repository_ingest_response(job)


@router.post("/ingest/{job_id}/cancel")
async def cancel_repository_ingest(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoIngestStatusResponse:
    job = await _get_authorized_repository_ingest(
        session,
        job_id,
        auth_user_id,
    )
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
        index_state = await _resolve_readable_index(
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
