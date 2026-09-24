from unittest.mock import DEFAULT, MagicMock, patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings


def _normalized_sql(connection: MagicMock) -> list[str]:
    return [
        " ".join(str(call.args[0]).split())
        for call in connection.execute.call_args_list
    ]


async def test_lifespan_provisions_schema_extras(monkeypatch):
    """create_all() cannot express the view or the composite/partial/vector
    indexes, so lifespan must create them explicitly on every startup."""
    monkeypatch.setattr(settings, "vector_type", "vector")
    monkeypatch.setattr(settings, "vector_index", "hnsw")
    connection = MagicMock()
    schema_events: list[str] = []

    def record_execute(statement):
        schema_events.append(" ".join(str(statement).split()))
        return DEFAULT

    def record_verification(_connection):
        schema_events.append("VERIFY_EMBEDDING_SCHEMA")

    connection.execute.side_effect = record_execute
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(main.SQLModel.metadata, "create_all") as create_all,
        patch.object(
            main,
            "verify_embedding_schema",
            side_effect=record_verification,
        ) as verify_schema,
    ):
        async with main.lifespan(main.app):
            pass

    create_all.assert_called_once_with(mock_engine)
    verify_schema.assert_called_once_with(connection)
    statements = _normalized_sql(connection)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in statements
    assert "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ref VARCHAR" in statements
    assert (
        "UPDATE jobs SET status = 'failed', "
        "error = 'Legacy issue brief is missing its issue repository; recreate it', "
        "claimed_at = NULL, claimed_by = NULL "
        "WHERE job_type = 'issue_brief' AND issue_repo IS NULL "
        "AND status IN ('pending', 'running')"
        in statements
    )
    assert (
        "CREATE INDEX IF NOT EXISTS ix_chunks_repo_generation "
        "ON code_chunks (repo_name, ref, generation)"
        in statements
    )
    assert any("column_name = 'installation_id'" in sql for sql in statements)
    assert "DROP VIEW IF EXISTS live_code_chunks" in statements
    assert any("CREATE OR REPLACE VIEW live_code_chunks AS" in sql for sql in statements)
    assert (
        'CREATE INDEX IF NOT EXISTS ix_jobs_pending '
        'ON jobs ("createdAt") WHERE status = \'pending\''
        in statements
    )
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_dedupe "
        "ON jobs (dedupe_key) WHERE status IN ('pending', 'running') "
        "AND dedupe_key IS NOT NULL"
        in statements
    )
    assert any(
        "CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw" in sql
        and "embedding vector_cosine_ops" in sql
        for sql in statements
    )
    hnsw_position = next(
        index
        for index, event in enumerate(schema_events)
        if "CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw" in event
    )
    assert schema_events.index("VERIFY_EMBEDDING_SCHEMA") < hnsw_position
    assert any(
        "CREATE INDEX IF NOT EXISTS ix_chunks_search" in sql
        for sql in statements
    )
    assert (
        "ALTER TABLE code_chunk_embeddings "
        "ALTER COLUMN embedding SET STORAGE PLAIN"
        in statements
    )


def test_embedding_index_ddl_uses_halfvec_operator_class(monkeypatch):
    monkeypatch.setattr(settings, "vector_type", "halfvec")
    monkeypatch.setattr(settings, "vector_index", "hnsw")

    ddl = main._embedding_index_ddl()

    assert ddl is not None
    assert "embedding halfvec_cosine_ops" in ddl


def test_embedding_index_ddl_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "vector_index", "none")

    assert main._embedding_index_ddl() is None


def test_lifespan_starts_and_stops_worker(monkeypatch):
    monkeypatch.setattr(settings, "run_worker", True)
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection

    async def fake_loop(stop_event):
        await stop_event.wait()

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(main.SQLModel.metadata, "create_all"),
        patch.object(main, "worker_loop", fake_loop),
        patch.object(main, "verify_embedding_schema"),
    ):
        with TestClient(main.app) as _client:
            task = main.app.state.worker_task
            assert task is not None
            assert not task.done()
        assert task.done()


def test_lifespan_skips_worker_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "run_worker", False)
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(main.SQLModel.metadata, "create_all"),
        patch.object(main, "verify_embedding_schema"),
    ):
        with TestClient(main.app) as _client:
            assert main.app.state.worker_task is None
