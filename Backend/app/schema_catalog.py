"""Compare a database schema with what the models and migrations describe.

``alembic check`` compares tables, columns and index columns, but not a
partial index's predicate, an index method (GIN vs GiST), views, or column
storage. This module compares the Postgres catalog directly: it builds the
expected schema in a scratch schema (the models plus the parts only the
migrations can express) and diffs the two catalogs.

Used by ``python -m app.migrate adopt`` and the migration tests.
"""

from __future__ import annotations

import uuid

from sqlalchemy import MetaData, text
from sqlalchemy.engine import Connection


# Created by hand when VECTOR_INDEX=hnsw; not owned by the migrations.
UNMANAGED_INDEXES = frozenset({"ix_embeddings_hnsw"})
IGNORED_TABLES = frozenset({"alembic_version"})


def _create_migration_only_parts(connection: Connection, schema: str) -> None:
    """The parts of the current schema that model metadata cannot express."""
    connection.execute(text(
        f"ALTER TABLE {schema}.code_chunk_embeddings "
        "ALTER COLUMN embedding SET STORAGE PLAIN"
    ))
    connection.execute(text(f"""
        CREATE VIEW {schema}.live_code_chunks AS
        SELECT c.*
        FROM {schema}.code_chunks c
        JOIN {schema}.repo_index_state s
          ON s.repo_name = c.repo_name
         AND s.ref = c.ref
         AND s.active_generation = c.generation
    """))


def _catalog(
    connection: Connection, schema: str, search_path: str
) -> dict[str, str]:
    """Every schema object of interest, keyed by a stable description.

    Postgres leaves names unqualified when they are visible on the search
    path, so with ``schema`` first the definitions of two schemas compare
    directly. Must run inside a transaction: the setting is local to it.
    """
    connection.execute(
        text("SELECT set_config('search_path', :path, true)"),
        {"path": f'"{schema}", {search_path}'},
    )
    entries: dict[str, str] = {}
    for name, kind in connection.execute(text("""
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'v', 'm', 'p')
    """), {"schema": schema}):
        if name not in IGNORED_TABLES:
            entries[f"relation {name}"] = {"v": "view", "m": "view"}.get(
                kind, "table"
            )

    for table, column, type_, not_null, default, storage in connection.execute(
        text("""
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                   a.attnotnull, pg_get_expr(d.adbin, d.adrelid), a.attstorage
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d
              ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE n.nspname = :schema
              AND c.relkind IN ('r', 'v', 'p')
              AND a.attnum > 0
              AND NOT a.attisdropped
        """),
        {"schema": schema},
    ):
        if table in IGNORED_TABLES:
            continue
        entries[f"column {table}.{column}"] = (
            f"{type_} not_null={not_null} default={default} storage={storage}"
        )

    # pg_get_indexdef always schema-qualifies the table, so indexes are
    # compared by their parts, each rendered relative to the search path.
    for (
        table, index, method, unique, columns, key_columns, opclasses,
        collations, options, nulls_not_distinct, predicate, usable,
    ) in connection.execute(text("""
            SELECT t.relname, i.relname, am.amname, x.indisunique,
                   (SELECT string_agg(pg_get_indexdef(x.indexrelid, k, true),
                                      ', ' ORDER BY k)
                    FROM generate_series(1, x.indnatts) AS k),
                   x.indnkeyatts,
                   (SELECT string_agg(opc.opcname, ', ' ORDER BY c.k)
                    FROM unnest(x.indclass) WITH ORDINALITY AS c(oid, k)
                    JOIN pg_opclass opc ON opc.oid = c.oid),
                   -- Collations decide what a unique index treats as equal.
                   (SELECT string_agg(
                        CASE WHEN c.oid = 0 THEN '-'
                             ELSE coll.collnamespace::regnamespace::text
                                  || '.' || quote_ident(coll.collname)
                        END, ', ' ORDER BY c.k)
                    FROM unnest(x.indcollation) WITH ORDINALITY AS c(oid, k)
                    LEFT JOIN pg_collation coll ON coll.oid = c.oid),
                   x.indoption::text,
                   x.indnullsnotdistinct,
                   pg_get_expr(x.indpred, x.indrelid, true),
                   x.indisvalid AND x.indisready
            FROM pg_index x
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_am am ON am.oid = i.relam
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = :schema
        """), {"schema": schema}):
        if table in IGNORED_TABLES or index in UNMANAGED_INDEXES:
            continue
        definition = (
            f"{'UNIQUE ' if unique else ''}ON {table} USING {method} "
            f"({columns}) keys={key_columns} opclasses=({opclasses}) "
            f"collations=({collations}) options={options}"
        )
        if nulls_not_distinct:
            definition += " NULLS NOT DISTINCT"
        if predicate is not None:
            definition += f" WHERE {predicate}"
        # A failed CREATE INDEX CONCURRENTLY leaves a correct-looking
        # definition behind that enforces nothing.
        if not usable:
            definition += " (INVALID: a failed build; drop and recreate it)"
        entries[f"index {index}"] = definition

    for table, constraint, definition in connection.execute(text("""
        SELECT t.relname, con.conname, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class t ON t.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = con.connamespace
        WHERE n.nspname = :schema
    """), {"schema": schema}):
        if table in IGNORED_TABLES:
            continue
        entries[f"constraint {table}.{constraint}"] = definition

    for view, definition in connection.execute(text("""
        SELECT c.relname, pg_get_viewdef(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND c.relkind = 'v'
    """), {"schema": schema}):
        entries[f"view {view}"] = " ".join(definition.split())

    return entries


def schema_differences(
    connection: Connection,
    metadata: MetaData | None = None,
) -> list[str]:
    """Differences between the connection's current schema and the expected one.

    Builds the expected schema inside a savepoint and rolls it back (with the
    search_path changes), so the caller's transaction is left as it was. The ``vector`` extension must
    already exist.
    """
    if metadata is None:
        from app.models.metadata import metadata

    actual_schema, search_path = connection.execute(
        text("SELECT current_schema(), current_setting('search_path')")
    ).one()
    expected_schema = f"camino_expected_{uuid.uuid4().hex[:12]}"
    savepoint = connection.begin_nested()
    try:
        connection.execute(text(f"CREATE SCHEMA {expected_schema}"))
        # execution_options() changes the connection in place, so the
        # translation is undone right after create_all.
        connection.execution_options(schema_translate_map={None: expected_schema})
        try:
            metadata.create_all(connection)
        finally:
            connection.execution_options(schema_translate_map=None)
        _create_migration_only_parts(connection, expected_schema)
        expected = _catalog(connection, expected_schema, search_path)
        actual = _catalog(connection, actual_schema, search_path)
    finally:
        savepoint.rollback()

    differences = []
    for key in sorted(expected.keys() | actual.keys()):
        if key not in actual:
            differences.append(f"missing {key}: {expected[key]}")
        elif key not in expected:
            differences.append(f"unexpected {key}: {actual[key]}")
        elif expected[key] != actual[key]:
            differences.append(
                f"{key} differs: expected {expected[key]}, found {actual[key]}"
            )
    return differences
