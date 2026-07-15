from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import exc

from app.rate_limit import (
    RateLimitDecision,
    consume_fixed_window,
    fixed_window_rate_limit,
)


def _session_returning(*, request_count: int, retry_after: int):
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.mappings.return_value.one.return_value = {
        "request_count": request_count,
        "retry_after": retry_after,
    }
    return session


def test_consume_fixed_window_allows_request_within_limit():
    session = _session_returning(request_count=2, retry_after=45)

    with patch("app.rate_limit.Session", return_value=session):
        decision = consume_fixed_window(
            bucket="agent_ask",
            user_id="user_123",
            request_limit=20,
            window_seconds=600,
        )

    assert decision == RateLimitDecision(allowed=True, retry_after=45)
    _, params = session.execute.call_args.args
    assert params == {
        "bucket": "agent_ask",
        "user_id": "user_123",
        "request_limit": 20,
        "window_seconds": 600,
    }
    session.commit.assert_called_once()


def test_consume_fixed_window_rejects_request_over_limit():
    session = _session_returning(request_count=21, retry_after=12)

    with patch("app.rate_limit.Session", return_value=session):
        decision = consume_fixed_window(
            bucket="agent_ask",
            user_id="user_123",
            request_limit=20,
            window_seconds=600,
        )

    assert decision == RateLimitDecision(allowed=False, retry_after=12)


def test_consume_fixed_window_fails_closed_when_database_is_unavailable():
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.side_effect = exc.OperationalError(
        "statement",
        {},
        RuntimeError("database unavailable"),
    )

    with (
        patch("app.rate_limit.Session", return_value=session),
        pytest.raises(HTTPException) as raised,
    ):
        consume_fixed_window(
            bucket="agent_ask",
            user_id="user_123",
            request_limit=20,
            window_seconds=600,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "Rate limit service unavailable"


@pytest.mark.asyncio
async def test_dependency_returns_retry_after_when_limit_is_exceeded():
    limiter = fixed_window_rate_limit(
        "agent_ask",
        request_limit=20,
        window_seconds=600,
    )

    with (
        patch(
            "app.rate_limit.consume_fixed_window",
            return_value=RateLimitDecision(allowed=False, retry_after=37),
        ),
        pytest.raises(HTTPException) as raised,
    ):
        await limiter(user_id="user_123")

    assert raised.value.status_code == 429
    assert raised.value.detail == "Rate limit exceeded. Try again later."
    assert raised.value.headers == {"Retry-After": "37"}


@pytest.mark.parametrize(
    ("request_limit", "window_seconds"),
    [(0, 60), (1, 0), (-1, 60), (1, -60)],
)
def test_fixed_window_requires_positive_values(request_limit, window_seconds):
    with pytest.raises(ValueError):
        fixed_window_rate_limit(
            "agent_ask",
            request_limit=request_limit,
            window_seconds=window_seconds,
        )
