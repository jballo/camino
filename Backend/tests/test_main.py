from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings


async def test_lifespan_only_checks_the_schema():
    """Migrations own the schema; startup checks it and changes nothing."""
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(main, "verify_schema") as verify_schema,
    ):
        async with main.lifespan(main.app):
            pass

    verify_schema.assert_called_once_with(connection)
    connection.execute.assert_not_called()
    connection.commit.assert_not_called()


async def test_lifespan_fails_when_the_schema_check_fails():
    mock_engine = MagicMock()

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(
            main, "verify_schema", side_effect=RuntimeError("schema is behind")
        ),
    ):
        try:
            async with main.lifespan(main.app):
                raise AssertionError("startup must not continue")
        except RuntimeError as error:
            assert str(error) == "schema is behind"


def test_lifespan_starts_and_stops_worker(monkeypatch):
    monkeypatch.setattr(settings, "run_worker", True)
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection

    async def fake_loop(stop_event):
        await stop_event.wait()

    with (
        patch.object(main, "engine", mock_engine),
        patch.object(main, "worker_loop", fake_loop),
        patch.object(main, "verify_schema"),
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
        patch.object(main, "verify_schema"),
    ):
        with TestClient(main.app) as _client:
            assert main.app.state.worker_task is None


def test_health_is_unauthenticated_and_touches_nothing():
    """The liveness probe must not run the startup check or need a database:
    a TestClient outside a ``with`` block never triggers lifespan."""
    mock_engine = MagicMock()
    with patch.object(main, "engine", mock_engine):
        response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_engine.connect.assert_not_called()


def test_health_is_not_part_of_the_public_schema():
    paths = TestClient(main.app).get("/openapi.json").json()["paths"]
    assert "/health" not in paths
