"""Canonical FTS ``search_vector`` definition for code chunks.

Kept in one place so the production ingest (``app.api.repositories``), the eval
ingest (``eval.ingest_local``), and the no-re-embed rebuild path all index text
identically. Rebuilding ``search_vector`` is a pure SQL ``UPDATE`` over existing
rows — it does **not** touch embeddings, so it is cheap to iterate on.

Tokenization rationale (see eval/EXPERIMENTS.md, Exp 1): code symbols like
``OAuth2PasswordBearer`` or ``get_openapi`` are single opaque tokens under the
default parser, so natural-language queries never match them. We split
snake_case and camelCase/acronym boundaries into separate words (indexed with
the ``english`` config so they align with the stemmed query), while also keeping
the raw symbol via the ``simple`` config for exact-symbol lookups.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session

# Split camelCase, PascalCase, ACRONYMBoundaries, and snake_case into words.
# Two passes: lower/digit -> Upper ("getOpenapi" -> "get Openapi") and
# Acronym -> Word ("HTTPBearer" -> "HTTP Bearer"), then underscores to spaces.
_SPLIT_SYMBOL = (
    r"regexp_replace("
    r"  regexp_replace("
    r"    regexp_replace(coalesce(symbol_name, ''), '([a-z0-9])([A-Z])', '\1 \2', 'g'),"
    r"    '([A-Z]+)([A-Z][a-z])', '\1 \2', 'g'),"
    r"  '_', ' ', 'g')"
)

# File path: drop separators, then apply the same camelCase split.
_SPLIT_PATH = (
    r"regexp_replace("
    r"  replace(replace(file_path, '/', ' '), '.', ' '),"
    r"  '([a-z0-9])([A-Z])', '\1 \2', 'g')"
)

# The full SET expression for ``code_chunks.search_vector``.
# A: symbol name (split + raw), B: file path, C: docstring.
SEARCH_VECTOR_EXPR = (
    f"setweight(to_tsvector('english', {_SPLIT_SYMBOL}), 'A') || "
    f"setweight(to_tsvector('simple',  coalesce(symbol_name, '')), 'A') || "
    f"setweight(to_tsvector('english', {_SPLIT_PATH}), 'B') || "
    f"setweight(to_tsvector('english', coalesce(docstring, '')), 'C')"
)


def populate_search_vector_sql(*, only_null: bool) -> str:
    """UPDATE statement that (re)builds ``search_vector`` for a repo.

    ``only_null=True`` is for first-time ingest (don't clobber existing rows);
    ``only_null=False`` recomputes every row (used by the FTS rebuild path).
    Bind params: ``repo_name`` and ``installation_id``.
    """
    guard = "AND search_vector IS NULL" if only_null else ""
    return f"""
        UPDATE code_chunks
        SET search_vector = {SEARCH_VECTOR_EXPR}
        WHERE repo_name = :repo_name
          AND installation_id = :installation_id
          {guard}
    """


def rebuild_search_vector(
    session: Session, repo_name: str, installation_id: int
) -> None:
    """Recompute ``search_vector`` for all chunks of a repo (no re-embedding)."""
    session.execute(
        text(populate_search_vector_sql(only_null=False)).bindparams(
            repo_name=repo_name, installation_id=installation_id
        )
    )
    session.commit()
