from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings


def _normalized_sql(connection: MagicMock) -> list[str]:
    return [
        " ".join(str(call.args[0]).split())
        for call in connection.execute.call_args_list
    ]


async def test_lifespan_migrates_github_user_id_for_existing_tables():
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
    assert (
        'ALTER TABLE githubconnections ADD COLUMN IF NOT EXISTS "githubUserId" INTEGER'
        in statements
    )
    assert (
        'CREATE INDEX IF NOT EXISTS "ix_githubconnections_githubUserId" '
        'ON githubconnections ("githubUserId")'
        in statements
    )
    assert "ALTER TABLE tour_jobs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ" in statements
    assert "ALTER TABLE tour_jobs ADD COLUMN IF NOT EXISTS claimed_by TEXT" in statements
    assert (
        "ALTER TABLE tour_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
        in statements
    )
    assert (
        'CREATE INDEX IF NOT EXISTS ix_tour_jobs_pending '
        'ON tour_jobs ("createdAt") WHERE status = \'pending\''
        in statements
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
