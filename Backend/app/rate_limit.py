from dataclasses import dataclass
import logging

from fastapi import Depends, HTTPException
from sqlalchemy import exc, text
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.security import get_authenticated_user_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int


_CONSUME_FIXED_WINDOW = text(
    """
    INSERT INTO rate_limits AS current (
        bucket,
        user_id,
        window_started_at,
        request_count
    )
    VALUES (
        :bucket,
        :user_id,
        statement_timestamp(),
        1
    )
    ON CONFLICT (bucket, user_id)
    DO UPDATE SET
        window_started_at = CASE
            WHEN current.window_started_at
                + make_interval(secs => :window_seconds)
                <= statement_timestamp()
            THEN statement_timestamp()
            ELSE current.window_started_at
        END,
        request_count = CASE
            WHEN current.window_started_at
                + make_interval(secs => :window_seconds)
                <= statement_timestamp()
            THEN 1
            ELSE LEAST(
                current.request_count + 1,
                CAST(:request_limit AS BIGINT) + 1
            )
        END
    RETURNING
        request_count,
        GREATEST(
            1,
            CEIL(
                EXTRACT(
                    EPOCH FROM (
                        window_started_at
                        + make_interval(secs => :window_seconds)
                        - statement_timestamp()
                    )
                )
            )
        )::INTEGER AS retry_after
    """
)


def consume_fixed_window(
    *,
    bucket: str,
    user_id: str,
    request_limit: int,
    window_seconds: int,
) -> RateLimitDecision:
    """Atomically consume one request from a PostgreSQL fixed window."""

    try:
        with Session(engine) as session:
            row = session.execute(
                _CONSUME_FIXED_WINDOW,
                {
                    "bucket": bucket,
                    "user_id": user_id,
                    "request_limit": request_limit,
                    "window_seconds": window_seconds,
                },
            ).mappings().one()
            session.commit()
    except exc.SQLAlchemyError as error:
        logger.exception(
            "rate limit database failure | bucket=%s user_id=%s",
            bucket,
            user_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Rate limit service unavailable",
        ) from error

    return RateLimitDecision(
        allowed=row["request_count"] <= request_limit,
        retry_after=row["retry_after"],
    )


def fixed_window_rate_limit(
    bucket: str,
    *,
    request_limit: int,
    window_seconds: int,
):
    """Create a Clerk-user-keyed FastAPI fixed-window dependency."""

    if request_limit <= 0 or window_seconds <= 0:
        raise ValueError("Rate limit and window must be positive")

    async def enforce(
        user_id: str = Depends(get_authenticated_user_id),
    ) -> None:
        decision = consume_fixed_window(
            bucket=bucket,
            user_id=user_id,
            request_limit=request_limit,
            window_seconds=window_seconds,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Try again later.",
                headers={"Retry-After": str(decision.retry_after)},
            )

    return enforce


AGENT_ASK_RATE_LIMIT = fixed_window_rate_limit(
    "agent_ask",
    request_limit=settings.rate_limit_agent_ask_requests,
    window_seconds=settings.rate_limit_agent_ask_window_seconds,
)
REPOSITORY_INGEST_RATE_LIMIT = fixed_window_rate_limit(
    "repository_ingest",
    request_limit=settings.rate_limit_repository_ingest_requests,
    window_seconds=settings.rate_limit_repository_ingest_window_seconds,
)
REPOSITORY_SEARCH_RATE_LIMIT = fixed_window_rate_limit(
    "repository_search",
    request_limit=settings.rate_limit_repository_search_requests,
    window_seconds=settings.rate_limit_repository_search_window_seconds,
)
JOURNEY_CREATE_RATE_LIMIT = fixed_window_rate_limit(
    "journey_create",
    request_limit=settings.rate_limit_journey_create_requests,
    window_seconds=settings.rate_limit_journey_create_window_seconds,
)
