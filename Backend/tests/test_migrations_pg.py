"""Migrations and the startup schema check against a real Postgres.

Each test gets its own empty scratch database on the server of
``TEST_DATABASE_URL`` (or the sanitized ``DATABASE_URL``), dropped afterwards.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from app import main, migrate, schema_revision
from app.config import settings
from app.db_schema import verify_schema
from app.models.metadata import metadata
from app.schema_catalog import _create_migration_only_parts, schema_differences
from app.schema_revision import (
    ALLOW_REVISION_ENV,
    MIGRATION_LOCK_ID,
    SchemaRevisionError,
    alembic_config,
    check_revision,
    head_revision,
    script_directory,
)
from tests.test_worker_claim_pg import database_unavailable


BACKEND_DIR = Path(__file__).resolve().parents[1]
HEAD = head_revision(script_directory())
TABLES = {
    "users",
    "githubconnections",
    "jobs",
    "rate_limits",
    "user_repo_follows",
    "repo_index_state",
    "code_chunks",
    "code_chunk_embeddings",
}


def _server_url() -> URL:
    override = os.environ.get("TEST_DATABASE_URL")
    return make_url(override or settings.database_url)


@pytest.fixture
def database_url():
    """An empty scratch database; yields its URL."""
    name = f"camino_migrate_test_{uuid.uuid4().hex}"
    admin = create_engine(
        _server_url().set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {name}"))
    except OperationalError as error:
        admin.dispose()
        database_unavailable(f"Postgres not reachable ({error})")
    try:
        yield _server_url().set(database=name)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def engine(database_url):
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


def _upgrade(engine) -> None:
    with engine.connect() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        config.attributes["configure_logger"] = False
        command.upgrade(config, "head")


def _set_revision(engine, revision: str) -> None:
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = :r"), {
            "r": revision
        })


def _revisions(engine) -> tuple[str, ...]:
    with engine.connect() as connection:
        return schema_revision.database_revisions(connection)


def _tables(engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars())


def _minimal_environment(database_url: URL) -> dict[str, str]:
    """Only what a migration container gets that matters: DATABASE_URL."""
    return {
        "PATH": os.environ["PATH"],
        "DATABASE_URL": database_url.render_as_string(hide_password=False),
    }


def test_upgrade_needs_only_database_url_and_persists(database_url, engine):
    result = subprocess.run(
        [sys.executable, "-m", "app.migrate"],
        cwd=BACKEND_DIR,
        env=_minimal_environment(database_url),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"schema is at {HEAD}" in result.stderr
    # Read back on a new connection: the upgrade committed.
    assert _revisions(engine) == (HEAD,)
    assert TABLES | {"alembic_version"} == _tables(engine)
    with engine.connect() as connection:
        assert schema_differences(connection) == []


def test_upgrade_reads_database_url_from_dotenv_like_the_app(
    database_url, engine, tmp_path
):
    # The documented local setup keeps settings in Backend/.env.
    (tmp_path / ".env").write_text(
        f"DATABASE_URL={database_url.render_as_string(hide_password=False)}\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "app.migrate"],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(BACKEND_DIR)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _revisions(engine) == (HEAD,)


def test_alembic_check_finds_no_difference_and_needs_only_database_url(
    database_url, engine
):
    _upgrade(engine)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=BACKEND_DIR,
        env=_minimal_environment(database_url),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout + result.stderr


def test_upgrade_twice_is_a_no_op(engine):
    _upgrade(engine)
    _upgrade(engine)

    assert _revisions(engine) == (HEAD,)


def test_catalog_check_sees_what_alembic_check_cannot(engine):
    _upgrade(engine)
    with engine.connect() as connection:
        connection.execute(text("DROP INDEX ix_jobs_pending"))
        connection.execute(text(
            "CREATE INDEX ix_jobs_pending ON jobs (\"createdAt\") "
            "WHERE status = 'running'"
        ))
        connection.execute(text("DROP INDEX ix_chunks_search"))
        connection.execute(text(
            "CREATE INDEX ix_chunks_search ON code_chunks USING gist (search_vector)"
        ))
        connection.execute(text(
            "ALTER TABLE code_chunk_embeddings "
            "ALTER COLUMN embedding SET STORAGE EXTENDED"
        ))
        connection.execute(text("DROP VIEW live_code_chunks"))

        differences = schema_differences(connection)
        connection.rollback()

    assert any(
        d.startswith("index ix_jobs_pending differs") and "'running'" in d
        for d in differences
    )
    assert any(
        d.startswith("index ix_chunks_search differs") and "USING gist" in d
        for d in differences
    )
    assert any(
        d.startswith("column code_chunk_embeddings.embedding differs")
        and "storage=x" in d
        for d in differences
    )
    assert any(d.startswith("missing view live_code_chunks") for d in differences)


def test_catalog_check_compares_predicate_literals_exactly(engine):
    # Schema names are left out of the definitions through search_path, never
    # by rewriting text, so a literal that looks like one still counts.
    _upgrade(engine)
    with engine.connect() as connection:
        connection.execute(text("DROP INDEX ix_jobs_pending"))
        connection.execute(text(
            "CREATE INDEX ix_jobs_pending ON jobs (\"createdAt\") "
            "WHERE status = 'public.pending'"
        ))

        differences = schema_differences(connection)
        connection.rollback()

    assert any(
        d.startswith("index ix_jobs_pending differs") and "public.pending" in d
        for d in differences
    )


def test_hand_made_hnsw_index_is_not_a_difference(engine):
    _upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX ix_embeddings_hnsw ON code_chunk_embeddings "
            "USING hnsw (embedding halfvec_cosine_ops)"
        ))

    with engine.connect() as connection:
        assert schema_differences(connection) == []
        connection.rollback()
        config = alembic_config()
        config.attributes["connection"] = connection
        config.attributes["configure_logger"] = False
        command.check(config)


def test_lock_timeout_rolls_back_and_retries_with_backoff(engine):
    _upgrade(engine)
    holder = engine.connect()
    holder.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
    delays: list[float] = []

    def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            holder.rollback()

    try:
        started = time.monotonic()
        with patch.object(schema_revision, "_sleep", sleep):
            _upgrade(engine)
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    # Two attempts each gave up after the 1 s lock timeout; the third ran.
    assert delays == [2, 4]
    assert 2 <= elapsed < 5
    assert _revisions(engine) == (HEAD,)


def test_lock_timeout_gives_up_after_bounded_attempts(engine):
    holder = engine.connect()
    holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    delays: list[float] = []
    try:
        with (
            patch.object(schema_revision, "MIGRATOR_WAIT", "100ms"),
            patch.object(schema_revision, "_sleep", delays.append),
            pytest.raises(OperationalError) as raised,
        ):
            _upgrade(engine)
    finally:
        holder.execute(text("SELECT pg_advisory_unlock_all()"))
        holder.close()

    assert raised.value.orig.pgcode == "55P03"
    assert delays == [2, 4, 8, 16]
    assert _tables(engine) == set()


def test_failed_upgrade_leaves_nothing_behind(engine):
    # The baseline creates every table, then fails on the view.
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE live_code_chunks (id int)"))

    with pytest.raises(ProgrammingError, match="live_code_chunks"):
        _upgrade(engine)

    assert _tables(engine) == {"live_code_chunks"}


def test_second_migrator_waits_for_the_first(engine):
    holder = engine.connect()
    holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _upgrade(engine)
        except BaseException as error:  # noqa: BLE001  (reported below)
            errors.append(error)

    migrator = threading.Thread(target=run)
    try:
        migrator.start()
        # Well past the 1 s DDL lock timeout: still waiting, not retrying.
        migrator.join(2.5)
        assert migrator.is_alive()
        assert _tables(engine) == set()
    finally:
        holder.execute(text("SELECT pg_advisory_unlock_all()"))
        holder.close()
    migrator.join(30)

    assert not migrator.is_alive()
    assert errors == []
    assert _revisions(engine) == (HEAD,)


def test_concurrent_migrators_both_succeed_once(database_url, engine):
    # Separate processes, as on a real box: Alembic's context is global
    # to a process.
    migrators = [
        subprocess.Popen(
            [sys.executable, "-m", "app.migrate"],
            cwd=BACKEND_DIR,
            env=_minimal_environment(database_url),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [migrator.communicate(timeout=120)[0] for migrator in migrators]

    assert [m.returncode for m in migrators] == [0, 0], "\n".join(outputs)
    # Exactly one of them ran the baseline; the other found nothing to do.
    assert sum("Running upgrade 20260928_01 -> " in out for out in outputs) == 1
    assert _revisions(engine) == (HEAD,)
    with engine.connect() as connection:
        assert schema_differences(connection) == []


def test_upgrade_refuses_an_unknown_revision_by_default(
    database_url, engine, monkeypatch
):
    _upgrade(engine)
    _set_revision(engine, "29990101_01")
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(False))
    monkeypatch.delenv(ALLOW_REVISION_ENV, raising=False)

    assert migrate.main([]) == 1

    monkeypatch.setenv(ALLOW_REVISION_ENV, "29990101_02")
    assert migrate.main([]) == 1
    assert _revisions(engine) == ("29990101_01",)


def test_upgrade_accepts_exactly_the_allowed_unknown_revision(
    database_url, engine, monkeypatch, caplog
):
    _upgrade(engine)
    _set_revision(engine, "29990101_01")
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(False))
    monkeypatch.setenv(ALLOW_REVISION_ENV, "29990101_01")

    with caplog.at_level(logging.INFO, logger="app"):
        assert migrate.main([]) == 0

    assert _revisions(engine) == ("29990101_01",)
    assert "nothing to migrate" in caplog.text
    assert "schema is at" not in caplog.text


def test_startup_check_by_revision(engine, monkeypatch):
    monkeypatch.delenv(ALLOW_REVISION_ENV, raising=False)
    with engine.connect() as connection:
        with pytest.raises(SchemaRevisionError, match="no schema revision"):
            check_revision(connection)

    _upgrade(engine)
    with engine.connect() as connection:
        assert check_revision(connection) == HEAD

    _set_revision(engine, migrate.PRE_BASELINE_REVISION)
    with engine.connect() as connection:
        with pytest.raises(SchemaRevisionError, match="behind"):
            check_revision(connection)

    _set_revision(engine, "29990101_01")
    with engine.connect() as connection:
        with pytest.raises(SchemaRevisionError, match=ALLOW_REVISION_ENV):
            check_revision(connection)
    monkeypatch.setenv(ALLOW_REVISION_ENV, "29990101_01")
    with engine.connect() as connection:
        assert check_revision(connection) == "29990101_01"


@pytest.fixture
def role_without_ddl(engine):
    """A login-less role that can read the schema but not change it."""
    role = f"camino_no_ddl_{uuid.uuid4().hex[:12]}"
    _upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        connection.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {role}"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        connection.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}"))
    yield role
    with engine.begin() as connection:
        connection.execute(text(f"DROP OWNED BY {role}"))
        connection.execute(text(f"DROP ROLE {role}"))


async def test_api_startup_runs_with_a_role_that_cannot_run_ddl(
    database_url, role_without_ddl
):
    restricted = create_engine(database_url)

    @event.listens_for(restricted, "connect")
    def _set_role(dbapi_connection, _record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET ROLE {role_without_ddl}")
        dbapi_connection.commit()

    try:
        with restricted.connect() as connection:
            with pytest.raises(ProgrammingError, match="permission denied"):
                connection.execute(text("CREATE TABLE ddl_probe (id int)"))
            connection.rollback()
            verify_schema(connection)
        with patch.object(main, "engine", restricted):
            async with main.lifespan(main.app):
                pass
    finally:
        restricted.dispose()


def _create_like_old_api_startup(engine) -> None:
    """The schema the API used to create at startup, at the 09-28 revision."""
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        metadata.create_all(connection)
        _create_migration_only_parts(connection, "public")
        connection.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        ))
        connection.execute(text(
            "INSERT INTO alembic_version VALUES (:r)"
        ), {"r": migrate.PRE_BASELINE_REVISION})


def test_adopt_stamps_a_matching_old_database(database_url, engine, monkeypatch):
    _create_like_old_api_startup(engine)
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(False))

    assert migrate.main(["adopt"]) == 0

    assert _revisions(engine) == (migrate.BASELINE_REVISION,)
    _upgrade(engine)
    assert _revisions(engine) == (HEAD,)


def test_adopt_refuses_a_different_schema_and_changes_nothing(
    database_url, engine, monkeypatch, caplog
):
    _create_like_old_api_startup(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_jobs_pending"))
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(False))

    assert migrate.main(["adopt"]) == 1

    assert "missing index ix_jobs_pending" in caplog.text
    assert _revisions(engine) == (migrate.PRE_BASELINE_REVISION,)


def test_adopt_refuses_an_index_left_invalid_by_a_failed_build(
    database_url, engine, monkeypatch, caplog
):
    _create_like_old_api_startup(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ux_jobs_active_dedupe"))
        for _ in range(2):
            connection.execute(text(
                "INSERT INTO jobs (repo_name, job_type, dedupe_key, status, "
                "attempts, refresh_cycles) "
                "VALUES ('org/repo', 'tour', 'same', 'pending', 0, 0)"
            ))
    autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
    with autocommit.connect() as connection:
        # The duplicates make the build fail, leaving an index with the right
        # definition that enforces nothing.
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "CREATE UNIQUE INDEX CONCURRENTLY ux_jobs_active_dedupe "
                "ON jobs (dedupe_key) WHERE status IN ('pending', 'running') "
                "AND dedupe_key IS NOT NULL"
            ))
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(False))

    assert migrate.main(["adopt"]) == 1

    assert "index ux_jobs_active_dedupe differs" in caplog.text
    assert "INVALID" in caplog.text
    assert _revisions(engine) == (migrate.PRE_BASELINE_REVISION,)


def test_adopt_refuses_a_unique_index_with_another_collation(
    database_url, engine, monkeypatch, caplog
):
    # Same columns and predicate, but a collation decides which dedupe keys
    # count as equal; the definition text alone would not show it.
    _create_like_old_api_startup(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ux_jobs_active_dedupe"))
        connection.execute(text(
            "CREATE UNIQUE INDEX ux_jobs_active_dedupe "
            "ON jobs (dedupe_key COLLATE \"C\") "
            "WHERE status IN ('pending', 'running') AND dedupe_key IS NOT NULL"
        ))
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(False))

    assert migrate.main(["adopt"]) == 1

    assert "index ux_jobs_active_dedupe differs" in caplog.text
    assert _revisions(engine) == (migrate.PRE_BASELINE_REVISION,)


def test_adopt_refuses_a_migrated_database(database_url, engine, monkeypatch):
    _upgrade(engine)
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(False))

    assert migrate.main(["adopt"]) == 1
    assert _revisions(engine) == (HEAD,)
