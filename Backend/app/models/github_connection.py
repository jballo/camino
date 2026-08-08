import datetime as dt
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, DateTime, func

class GithubConnections(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    userId: str = Field(unique=True)
    githubUsername: str
    githubUserId: int = Field(index=True)
    installationId: int
    encryptedAccessToken: str
    encryptedRefreshToken: str
    tokenExpiresAt: dt.datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    refreshTokenExpiresAt: dt.datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
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