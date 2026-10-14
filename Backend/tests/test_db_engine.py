"""Engine settings that bound dead connections and lock waits."""

import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app import db
from app.config import settings
from app.db import engine_options
from tests.test_worker_claim_pg import pg_engine  # noqa: F401  (fixture)


def test_engine_bounds_connects_dead_connections_and_lock_waits():
    options = engine_options(settings)

    assert options["pool_pre_ping"] is True
    assert options["connect_args"] == {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "options": "-c lock_timeout=30000",
    }
    assert db.engine.pool._pre_ping is True


def test_engine_options_follow_settings():
    config = settings.model_copy(
        update={
            "database_connect_timeout": 3,
            "database_keepalives_idle": 5,
            "database_keepalives_interval": 2,
            "database_keepalives_count": 4,
            "database_lock_timeout_ms": 0,
        }
    )

    connect_args = engine_options(config)["connect_args"]

    assert connect_args["connect_timeout"] == 3
    assert connect_args["keepalives_idle"] == 5
    assert connect_args["keepalives_interval"] == 2
    assert connect_args["keepalives_count"] == 4
    assert connect_args["options"] == "-c lock_timeout=0"


def test_postgres_accepts_the_connection_settings(pg_engine):  # noqa: F811
    engine = create_engine(pg_engine.url, **engine_options(settings))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW lock_timeout")).scalar() == "30s"
    finally:
        engine.dispose()


def test_lock_wait_fails_instead_of_hanging(pg_engine):  # noqa: F811
    config = settings.model_copy(update={"database_lock_timeout_ms": 500})
    engine = create_engine(pg_engine.url, **engine_options(config))
    with pg_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS lock_probe (id int)"))
    try:
        with pg_engine.connect() as holder:
            holder.execute(text("LOCK TABLE lock_probe IN ACCESS EXCLUSIVE MODE"))
            started = time.monotonic()
            with pytest.raises(OperationalError) as raised:
                with engine.connect() as waiter:
                    waiter.execute(text("SELECT * FROM lock_probe"))
            elapsed = time.monotonic() - started
            holder.rollback()
    finally:
        engine.dispose()

    assert raised.value.orig.pgcode == "55P03"  # lock_not_available
    assert elapsed < 5
