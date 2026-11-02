"""Engine settings that bound dead connections and lock waits."""

import time
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app import db, main
from app.config import Settings, settings
from app.db import engine_options
from tests.test_worker_claim_pg import pg_engine  # noqa: F401  (fixture)


TIMEOUT_FIELDS = (
    "database_connect_timeout",
    "database_keepalives_idle",
    "database_keepalives_interval",
    "database_keepalives_count",
    "database_lock_timeout_ms",
)


def _default_settings(**overrides) -> Settings:
    """Shared settings with the timeout defaults, whatever the local env sets."""
    defaults = {name: Settings.model_fields[name].default for name in TIMEOUT_FIELDS}
    return settings.model_copy(update={**defaults, **overrides})


def test_engine_bounds_connects_dead_connections_and_lock_waits():
    options = engine_options(_default_settings())

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
    config = _default_settings(
        database_connect_timeout=3,
        database_keepalives_idle=5,
        database_keepalives_interval=2,
        database_keepalives_count=4,
        database_lock_timeout_ms=0,
    )

    connect_args = engine_options(config)["connect_args"]

    assert connect_args["connect_timeout"] == 3
    assert connect_args["keepalives_idle"] == 5
    assert connect_args["keepalives_interval"] == 2
    assert connect_args["keepalives_count"] == 4
    assert connect_args["options"] == "-c lock_timeout=0"


def test_engine_keeps_options_from_the_database_url():
    config = _default_settings(
        database_url="postgresql://localhost/camino?options=-csearch_path%3Dcamino"
    )

    connect_args = engine_options(config)["connect_args"]

    assert connect_args["options"] == "-csearch_path=camino -c lock_timeout=30000"


def test_postgres_accepts_the_connection_settings(pg_engine):  # noqa: F811
    engine = create_engine(pg_engine.url, **engine_options(_default_settings()))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW lock_timeout")).scalar() == "30s"
    finally:
        engine.dispose()


def test_postgres_keeps_the_schema_from_the_database_url(pg_engine):  # noqa: F811
    url = pg_engine.url.update_query_dict({"options": "-csearch_path=camino"})
    config = _default_settings(
        database_url=url.render_as_string(hide_password=False)
    )
    engine = create_engine(make_url(config.database_url), **engine_options(config))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW search_path")).scalar() == "camino"
            assert conn.execute(text("SHOW lock_timeout")).scalar() == "30s"
    finally:
        engine.dispose()


def test_lock_wait_fails_instead_of_hanging(pg_engine):  # noqa: F811
    config = _default_settings(database_lock_timeout_ms=500)
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


async def test_startup_does_not_wait_for_locks_on_application_tables(pg_engine):  # noqa: F811
    config = _default_settings(database_lock_timeout_ms=500)
    engine = create_engine(pg_engine.url, **engine_options(config))
    # Startup used to run DDL that queued behind any transaction on jobs. The
    # check only reads the catalog and alembic_version, so even the strongest
    # lock on jobs does not hold it up.
    holder = pg_engine.connect()
    holder.execute(text("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE"))
    try:
        started = time.monotonic()
        with patch.object(main, "engine", engine):
            async with main.lifespan(main.app):
                pass
        elapsed = time.monotonic() - started
        with engine.connect() as conn:
            lock_timeout = conn.execute(text("SHOW lock_timeout")).scalar()
            statement_timeout = conn.execute(text("SHOW statement_timeout")).scalar()
    finally:
        holder.rollback()
        holder.close()
        engine.dispose()

    assert elapsed < 0.5
    # The check's SET LOCAL timeouts ended with its transaction.
    assert lock_timeout == "500ms"
    assert statement_timeout == "0"
