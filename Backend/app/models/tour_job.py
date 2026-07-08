import datetime as dt

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB


class TourJobStatus:
    """Job lifecycle states (see docs/tour-generation.md §6)."""

    PENDING = "pending"
    GENERATING = "generating"
    COMPLETE = "complete"
    FAILED = "failed"


class TourJob(SQLModel, table=True):
    __tablename__ = "tour_jobs"

    id: int | None = Field(default=None, primary_key=True)
    userId: str = Field(index=True)
    installation_id: int
    repo_name: str = Field(index=True)
    topic: str
    status: str = Field(default=TourJobStatus.PENDING, index=True)
    artifact: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    error: str | None = None
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
