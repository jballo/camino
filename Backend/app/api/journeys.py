import datetime as dt
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc
from sqlmodel import Session, select

from app.db import SessionDep, engine
from app.models.github_connection import GithubConnections
from app.models.tour_job import TourJob, TourJobStatus
from app.rate_limit import JOURNEY_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.tour import TourGenerationError, generate_tour

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateJourneyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repoName: str
    topic: str = Field(min_length=1, max_length=500)


class JourneyCreatedResponse(BaseModel):
    id: int
    status: str


class JourneyResponse(BaseModel):
    id: int
    status: str
    repoName: str
    topic: str
    artifact: dict | None = None
    error: str | None = None


class JourneySummaryResponse(BaseModel):
    id: int
    status: str
    repoName: str
    topic: str
    createdAt: dt.datetime


async def _run_generation(job_id: int) -> None:
    """Background worker: generate a tour and persist the outcome on the job row.

    Runs after the HTTP response is sent, so it owns its own DB session (the
    request-scoped session is already closed by then).
    """
    with Session(engine) as session:
        job = session.get(TourJob, job_id)
        if job is None:
            logger.error("tour job vanished before generation | id=%s", job_id)
            return

        job.status = TourJobStatus.GENERATING
        session.add(job)
        session.commit()
        session.refresh(job)

        topic, repo_name, installation_id = job.topic, job.repo_name, job.installation_id

        try:
            artifact = await generate_tour(
                session,
                topic=topic,
                repo_name=repo_name,
                installation_id=installation_id,
            )
        except TourGenerationError as e:
            logger.warning(
                "tour generation failed | id=%s topic=%r repo=%r: %s",
                job_id, topic, repo_name, e,
            )
            session.rollback()
            _mark_failed(session, job_id, str(e))
            return
        except Exception:
            logger.exception(
                "tour generation crashed | id=%s topic=%r repo=%r",
                job_id, topic, repo_name,
            )
            session.rollback()
            _mark_failed(session, job_id, "Internal generation error")
            return

        job = session.get(TourJob, job_id)
        if job is None:
            logger.error("tour job vanished after generation | id=%s", job_id)
            return
        job.status = TourJobStatus.COMPLETE
        job.artifact = artifact.model_dump()
        job.error = None
        session.add(job)
        try:
            session.commit()
        except exc.SQLAlchemyError:
            logger.exception(
                "tour job commit failed after successful generation | id=%s topic=%r repo=%r",
                job_id, topic, repo_name,
            )
            session.rollback()
            _mark_failed(session, job_id, "Failed to persist generated tour")
            return
        logger.info(
            "tour generation complete | id=%s topic=%r steps=%d",
            job_id, topic, len(artifact.steps),
        )


def _mark_failed(session: Session, job_id: int, error: str) -> None:
    try:
        job = session.get(TourJob, job_id)
        if job is None:
            return
        job.status = TourJobStatus.FAILED
        job.error = error
        session.add(job)
        session.commit()
    except exc.SQLAlchemyError:
        logger.exception(
            "failed to mark tour job as FAILED | id=%s", job_id
        )
        session.rollback()


@router.post("", dependencies=[Depends(JOURNEY_CREATE_RATE_LIMIT)])
async def create_journey(
    payload: CreateJourneyBody,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> JourneyCreatedResponse:
    try:
        gh_connection = session.exec(
            select(GithubConnections).where(
                GithubConnections.userId == auth_user_id
            )
        ).one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        job = TourJob(
            userId=auth_user_id,
            installation_id=gh_connection.installationId,
            repo_name=payload.repoName,
            topic=payload.topic,
            status=TourJobStatus.PENDING,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    background_tasks.add_task(_run_generation, job.id)
    logger.info(
        "tour job queued | id=%s topic=%r repo=%r", job.id, payload.topic, payload.repoName
    )
    return JourneyCreatedResponse(id=job.id, status=job.status)


@router.get("/{job_id}")
async def get_journey(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> JourneyResponse:
    try:
        job = session.get(TourJob, job_id)
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if job is None:
        raise HTTPException(status_code=404, detail="Journey not found")
    if job.userId != auth_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    return JourneyResponse(
        id=job.id,
        status=job.status,
        repoName=job.repo_name,
        topic=job.topic,
        artifact=job.artifact,
        error=job.error,
    )


@router.get("")
async def list_journeys(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
    repo: str | None = None,
) -> list[JourneySummaryResponse]:
    statement = select(TourJob).where(TourJob.userId == auth_user_id)
    if repo:
        statement = statement.where(TourJob.repo_name == repo)
    statement = statement.order_by(TourJob.createdAt.desc())

    try:
        jobs = session.exec(statement).all()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    return [
        JourneySummaryResponse(
            id=job.id,
            status=job.status,
            repoName=job.repo_name,
            topic=job.topic,
            createdAt=job.createdAt,
        )
        for job in jobs
    ]
