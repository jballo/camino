import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc
from sqlmodel import select

from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.models.tour_job import TourJob, TourJobStatus
from app.rate_limit import JOURNEY_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id

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


@router.post("", dependencies=[Depends(JOURNEY_CREATE_RATE_LIMIT)])
async def create_journey(
    payload: CreateJourneyBody,
    session: SessionDep,
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
