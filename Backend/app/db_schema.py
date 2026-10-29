import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import settings
from app.schema_revision import check_revision
from app.services.embeddings import EMBED_DIMENSIONS


logger = logging.getLogger(__name__)

# Startup only reads the catalog. The timeout keeps a start from hanging
# behind a lock held by a running migration.
_STARTUP_CHECK_TIMEOUT = "10s"


def verify_schema(conn: Connection) -> None:
    """Fail fast unless the database is at this release's schema revision and
    its embedding column matches VECTOR_TYPE. Runs no DDL."""
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(text(f"SET LOCAL statement_timeout = '{_STARTUP_CHECK_TIMEOUT}'"))
    conn.execute(text(f"SET LOCAL lock_timeout = '{_STARTUP_CHECK_TIMEOUT}'"))
    try:
        check_revision(conn)
        verify_embedding_schema(conn)
    finally:
        conn.rollback()


def verify_embedding_schema(conn: Connection) -> None:
    """Fail fast when runtime vector settings disagree with the database."""
    actual_type = conn.execute(text("""
        SELECT format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid = to_regclass('code_chunk_embeddings')
          AND attname = 'embedding'
          AND NOT attisdropped
    """)).scalar_one_or_none()
    expected_type = f"{settings.vector_type}({EMBED_DIMENSIONS})"

    if actual_type != expected_type:
        displayed_type = actual_type if actual_type is not None else "missing"
        raise RuntimeError(
            "Embedding schema mismatch: "
            f"database column type is {displayed_type!r}, "
            f"but configuration expects {expected_type!r}. "
            "Set VECTOR_TYPE to match the database, or recreate the local "
            "database (Backend/README.md, 'Migrations')."
        )

    hnsw_index = conn.execute(
        text("SELECT to_regclass('ix_embeddings_hnsw')")
    ).scalar_one_or_none()
    if settings.vector_index == "none" and hnsw_index is not None:
        logger.warning(
            "ix_embeddings_hnsw exists but VECTOR_INDEX=none; "
            "the index is unused storage"
        )
    elif settings.vector_index == "hnsw" and hnsw_index is None:
        logger.warning(
            "VECTOR_INDEX=hnsw but ix_embeddings_hnsw does not exist; search "
            "scans every embedding. Create it by hand (Backend/README.md, "
            "'Vector index')"
        )
