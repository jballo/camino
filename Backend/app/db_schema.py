import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import settings
from app.services.embeddings import EMBED_DIMENSIONS


logger = logging.getLogger(__name__)


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
            "Run the one-off SQL in Backend/README.md under "
            "'Local DB created before 2026-09', or recreate the local volume."
        )

    hnsw_index = conn.execute(
        text("SELECT to_regclass('ix_embeddings_hnsw')")
    ).scalar_one_or_none()
    if settings.vector_index == "none" and hnsw_index is not None:
        logger.warning(
            "ix_embeddings_hnsw exists but VECTOR_INDEX=none; "
            "the index is unused storage"
        )
