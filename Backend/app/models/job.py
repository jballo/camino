import datetime as dt

from sqlalchemy import Column, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class JobType:
    TOUR = "tour"
    REPOSITORY_INGEST = "repository_ingest"
    ISSUE_BRIEF = "issue_brief"


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    GENERATING = RUNNING  # Backward-compatible name for older callers.
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ACTIVE = (PENDING, RUNNING)


class Job(SQLModel, table=True):
    """A durable unit of background work handled by the shared worker."""

    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    userId: str = Field(index=True)
    installation_id: int
    repo_name: str = Field(index=True)
    ref: str | None = Field(default=None, index=True)
    issue_repo: str | None = Field(default=None)
    issue_number: int | None = Field(default=None, index=True)
    job_type: str = Field(default=JobType.TOUR, index=True)
    dedupe_key: str | None = Field(default=None)
    topic: str | None = Field(default=None)
    status: str = Field(default=JobStatus.PENDING, index=True)
    artifact: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    error: str | None = None
    claimed_at: dt.datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    claimed_by: str | None = Field(default=None)
    attempts: int = Field(default=0)
    blocked_by_job_id: int | None = Field(default=None, index=True)
    refresh_cycles: int = Field(default=0)
    createdAt: dt.datetime = Field(
        sa_column=Column[dt.datetime](
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        )
    )
    updatedAt: dt.datetime = Field(
        sa_column=Column[dt.datetime](
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )
