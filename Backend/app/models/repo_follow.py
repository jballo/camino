import datetime as dt

from sqlalchemy import Column, DateTime, UniqueConstraint, func
from sqlmodel import Field, SQLModel


class UserRepoFollow(SQLModel, table=True):
    __tablename__ = "user_repo_follows"
    __table_args__ = (
        UniqueConstraint("userId", "repo_name", name="uq_user_repo_follow"),
    )

    id: int | None = Field(default=None, primary_key=True)
    userId: str = Field(index=True)
    repo_name: str = Field(index=True)
    createdAt: dt.datetime = Field(
        sa_column=Column[dt.datetime](
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        )
    )
