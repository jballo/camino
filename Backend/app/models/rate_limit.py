import datetime as dt

from sqlalchemy import BigInteger, Column, DateTime
from sqlmodel import Field, SQLModel


class RateLimit(SQLModel, table=True):
    """Current fixed-window counter for one user and endpoint bucket."""

    __tablename__ = "rate_limits"

    bucket: str = Field(primary_key=True, max_length=100)
    user_id: str = Field(primary_key=True, max_length=255)
    window_started_at: dt.datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    request_count: int = Field(
        default=1,
        sa_column=Column(BigInteger, nullable=False),
    )
