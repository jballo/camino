import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _disable_in_process_worker():
    """Keep route tests from starting the polling loop via lifespan."""
    original = settings.run_worker
    settings.run_worker = False
    yield
    settings.run_worker = original
