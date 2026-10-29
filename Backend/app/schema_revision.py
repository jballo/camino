"""Schema revisions: how migrations run, and the check the API and worker make.

Only Alembic changes the schema (``python -m app.migrate``, a one-off step
before each deploy). The API and worker read the database's revision at
startup and refuse to run against a schema they were not built for.

Nothing here reads application settings: migrations need only DATABASE_URL.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError


logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Lets a rolled-back release run against the newer schema of the release it
# replaced. Set it to exactly the database's revision; anything else unknown
# still fails (docs/deploy.md, "Rolling back").
ALLOW_REVISION_ENV = "CAMINO_ALLOW_SCHEMA_REVISION"

# A DDL statement waits this long to get a lock another session holds (an API
# request or a worker job), then the whole upgrade rolls back and is tried
# again. Queries that queue behind the waiting DDL wait at most this long too.
# A lock once granted is held until the upgrade's transaction ends.
LOCK_TIMEOUT = "1s"
# Seconds to wait before each retry; one more attempt than delays.
RETRY_DELAYS = (2, 4, 8, 16)
# Bounds one statement, waiting included; nothing bounds the whole upgrade.
STATEMENT_TIMEOUT = "2min"
# How long a second migrator waits for the first to finish.
MIGRATOR_WAIT = "2min"
# pg_advisory_xact_lock key that serializes migrators ("camino" in ASCII).
MIGRATION_LOCK_ID = 0x63616D696E6F

LOCK_NOT_AVAILABLE = "55P03"


class SchemaRevisionError(RuntimeError):
    """The database is not at a revision this code can run against."""


class _DatabaseSettings(BaseSettings):
    """DATABASE_URL alone, found where app/config.py finds it: the
    environment first, then .env in the working directory."""

    database_url: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def database_url() -> str:
    url = _DatabaseSettings().database_url
    if not url:
        raise SchemaRevisionError(
            "DATABASE_URL is not set (in the environment or in .env in the "
            "working directory)."
        )
    return url


def alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


def head_revision(script: ScriptDirectory) -> str:
    heads = script.get_heads()
    if len(heads) != 1:
        raise SchemaRevisionError(
            f"The migrations have {len(heads)} heads ({', '.join(heads)}); "
            "merge them into one with `alembic merge` before deploying."
        )
    return heads[0]


def is_known_revision(script: ScriptDirectory, revision: str) -> bool:
    try:
        return script.get_revision(revision) is not None
    except (CommandError, RevisionError):
        return False


def database_revisions(connection: Connection) -> tuple[str, ...]:
    """The revisions stamped in alembic_version; empty if it does not exist."""
    exists = connection.execute(
        text("SELECT to_regclass('alembic_version')")
    ).scalar_one_or_none()
    if exists is None:
        return ()
    return tuple(
        connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
    )


def unknown_revision_allowed(revision: str) -> bool:
    return os.environ.get(ALLOW_REVISION_ENV, "").strip() == revision


def _unknown_revision_message(revision: str) -> str:
    return (
        f"The database is at revision {revision!r}, which this release does "
        "not know: a newer release migrated it (a rollback?), or this is not "
        "the Camino database. To run this release against it anyway, set "
        f"{ALLOW_REVISION_ENV}={revision} (docs/deploy.md, 'Rolling back')."
    )


def check_revision(
    connection: Connection,
    script: ScriptDirectory | None = None,
) -> str:
    """Return the database revision, or raise unless this code can run on it.

    Read-only. Passes at the code's head revision, or at the unknown revision
    named by CAMINO_ALLOW_SCHEMA_REVISION.
    """
    script = script if script is not None else script_directory()
    head = head_revision(script)
    revisions = database_revisions(connection)
    if not revisions:
        raise SchemaRevisionError(
            "The database has no schema revision. Run `python -m app.migrate` "
            "before starting the API or worker (docs/deploy.md); a database "
            "created by an older API startup needs `python -m app.migrate "
            "adopt` instead (Backend/README.md, 'Migrations')."
        )
    if len(revisions) > 1:
        raise SchemaRevisionError(
            f"The database has {len(revisions)} schema revisions "
            f"({', '.join(revisions)}); expected exactly one."
        )
    (revision,) = revisions
    if revision == head:
        return revision
    if is_known_revision(script, revision):
        raise SchemaRevisionError(
            f"The database is at revision {revision!r}, behind this "
            f"release's {head!r}. Run `python -m app.migrate` first."
        )
    if unknown_revision_allowed(revision):
        logger.warning(
            "running against unknown schema revision %s (allowed by %s); "
            "this release expects %s",
            revision,
            ALLOW_REVISION_ENV,
            head,
        )
        return revision
    raise SchemaRevisionError(_unknown_revision_message(revision))


def _pgcode(error: OperationalError) -> str | None:
    return getattr(error.orig, "pgcode", None)


_sleep = time.sleep


def run_migrations(connection: Connection, migrate: Callable[[], None]) -> None:
    """Run ``migrate`` in one transaction that never waits long for a lock.

    Called from migrations/env.py; ``migrate`` configures Alembic on
    ``connection`` and runs the migrations. This function owns the
    transaction: Alembic sees it as external, so it neither commits nor
    rolls back on its own, and a failure leaves nothing behind.

    A lock timeout rolls everything back and retries the whole upgrade after
    a delay. That is only safe while every migration is transactional; a
    migration that needs ``op.get_context().autocommit_block()`` (for example
    CREATE INDEX CONCURRENTLY) commits part of the work and needs its own
    restartable procedure before it can run here.
    """
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            with connection.begin():
                connection.execute(
                    text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                )
                # Wait for any other migrator, then read the revision only
                # once nobody else can change it.
                connection.execute(text(f"SET LOCAL lock_timeout = '{MIGRATOR_WAIT}'"))
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:id)"),
                    {"id": MIGRATION_LOCK_ID},
                )
                connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                if _skip_unknown_revision(connection):
                    return
                migrate()
                revisions = database_revisions(connection)
            logger.info("schema is at %s", ", ".join(revisions) or "no revision")
            return
        except OperationalError as error:
            if _pgcode(error) != LOCK_NOT_AVAILABLE or attempt == attempts:
                raise
            delay = RETRY_DELAYS[attempt - 1]
            logger.warning(
                "migration attempt %d/%d waited more than %s for a lock and "
                "was rolled back; retrying in %ss",
                attempt,
                attempts,
                LOCK_TIMEOUT,
                delay,
            )
            _sleep(delay)


def _skip_unknown_revision(connection: Connection) -> bool:
    """True when the database is at the allowed unknown revision."""
    revisions = database_revisions(connection)
    if len(revisions) > 1:
        raise SchemaRevisionError(
            f"The database has {len(revisions)} schema revisions "
            f"({', '.join(revisions)}); expected exactly one."
        )
    if not revisions:
        return False
    (revision,) = revisions
    script = script_directory()
    head_revision(script)
    if is_known_revision(script, revision):
        return False
    if unknown_revision_allowed(revision):
        logger.warning(
            "database is at unknown revision %s, allowed by %s; nothing to "
            "migrate",
            revision,
            ALLOW_REVISION_ENV,
        )
        return True
    raise SchemaRevisionError(_unknown_revision_message(revision))
