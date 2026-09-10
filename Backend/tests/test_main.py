from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings


def _normalized_sql(connection: MagicMock) -> list[str]:
    return [
        " ".join(str(call.args[0]).split())
        for call in connection.execute.call_args_list
    ]


async def test_lifespan_provisions_schema_extras():
    """create_all() cannot express the view or the composite/partial/vector
    indexes, so lifespan must create them explicitly on every startup."""
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(main.SQLModel.metadata, "create_all") as create_all,
    ):
        async with main.lifespan(main.app):
            pass

    create_all.assert_called_once_with(mock_engine)
    statements = _normalized_sql(connection)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in statements
    assert (
        "CREATE INDEX IF NOT EXISTS ix_chunks_repo_generation "
        "ON code_chunks (installation_id, repo_name, generation)"
        in statements
    )
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
        for sql in statements
    )
    assert any(
        "CREATE INDEX IF NOT EXISTS ix_chunks_search" in sql
        for sql in statements
    )


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
    ):
        with TestClient(main.app) as _client:
            assert main.app.state.worker_task is None
