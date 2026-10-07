import datetime as dt

from sqlalchemy import Boolean, Column, DateTime, func, true
from sqlmodel import Field, SQLModel


class GithubConnections(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    userId: str = Field(unique=True)
    githubUsername: str
    githubUserId: int = Field(index=True)
    installationId: int
    active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=true()),
    )
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
