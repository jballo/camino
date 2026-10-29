"""Apply schema migrations; needs only DATABASE_URL.

    python -m app.migrate            upgrade the database to this release
    python -m app.migrate current    print the database and release revisions
    python -m app.migrate adopt      stamp a database created by the old API
                                     startup, after checking its schema

Deploys run the upgrade as a one-off step before starting the API and worker
(docs/deploy.md). Lock timeouts, retries and the migrator lock live in
app/schema_revision.py.
"""

from __future__ import annotations

import argparse
import logging
import sys

from alembic import command
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import Connection

from app.schema_catalog import schema_differences
from app.schema_revision import (
    LOCK_TIMEOUT,
    MIGRATION_LOCK_ID,
    SchemaRevisionError,
    alembic_config,
    database_revisions,
    database_url,
    head_revision,
    script_directory,
)


logger = logging.getLogger("app.migrate")

# The revision that first creates the whole schema, and the only revision a
# database created by the old API startup can carry.
BASELINE_REVISION = "20261029_01"
PRE_BASELINE_REVISION = "20260928_01"


def upgrade() -> None:
    head_revision(script_directory())
    config = alembic_config()
    # main() has configured logging already.
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def current(connection: Connection) -> None:
    revisions = database_revisions(connection) or ("none",)
    print(f"database: {', '.join(revisions)}")
    print(f"release:  {head_revision(script_directory())}")


def adopt(connection: Connection) -> None:
    """Stamp the baseline on a database that API startup created.

    Refuses unless its schema matches the baseline exactly (data is never
    changed); the differences are printed so they can be fixed by hand or the
    database recreated.
    """
    head = head_revision(script_directory())
    if head != BASELINE_REVISION:
        raise SchemaRevisionError(
            f"This release's schema ({head}) is past the baseline "
            f"({BASELINE_REVISION}). Adopt with the release that added the "
            "baseline, then run `python -m app.migrate`, or recreate the "
            "database."
        )
    with connection.begin():
        connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        locked = connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID}
        ).scalar_one()
        if not locked:
            raise SchemaRevisionError(
                "Another migration is running; adopt again once it finishes."
            )
        revisions = database_revisions(connection)
        if revisions not in ((), (PRE_BASELINE_REVISION,)):
            raise SchemaRevisionError(
                f"The database is already at {', '.join(revisions)}; there is "
                "nothing to adopt. Run `python -m app.migrate`."
            )
        has_jobs = connection.execute(
            text("SELECT to_regclass('jobs')")
        ).scalar_one_or_none()
        if has_jobs is None:
            raise SchemaRevisionError(
                "The database has no application tables; there is nothing to "
                "adopt. Run `python -m app.migrate` to create the schema."
            )
        differences = schema_differences(connection)
        if differences:
            raise SchemaRevisionError(
                "The schema differs from the baseline, so it was not adopted:\n"
                + "\n".join(f"  - {difference}" for difference in differences)
                + "\nFix these by hand and adopt again, or recreate the "
                "database (Backend/README.md, 'Migrations')."
            )
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        ))
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": BASELINE_REVISION},
        )
    logger.info("adopted: the database is stamped at %s", BASELINE_REVISION)


def _connect_and_run(action) -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            action(connection)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrate")
    parser.add_argument(
        "action",
        nargs="?",
        default="upgrade",
        choices=("upgrade", "current", "adopt"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    try:
        if args.action == "upgrade":
            upgrade()
        elif args.action == "current":
            _connect_and_run(current)
        else:
            _connect_and_run(adopt)
    except SchemaRevisionError as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
