import base64
import os

import pytest
from sqlalchemy.engine import make_url


configured_database_url = os.environ.get("DATABASE_URL")
configured_database_name = (
    make_url(configured_database_url).database
    if configured_database_url
    else None
)
del configured_database_url

os.environ["DATABASE_URL"] = "postgresql://localhost/camino_test"
os.environ["CLERK_WH_KEY"] = "SYNTHETIC_TEST_VALUE"
os.environ["CLERK_SECRET_KEY"] = "SYNTHETIC_TEST_VALUE"
os.environ["GH_APP_ID"] = "1"
os.environ["GH_APP_CLIENT_ID"] = "SYNTHETIC_TEST_VALUE"
os.environ["GH_APP_SECRET"] = "SYNTHETIC_TEST_VALUE"
os.environ["GH_APP_PRIVATE_KEY"] = "SYNTHETIC_TEST_VALUE"
os.environ["ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"SYNTHETIC_TEST_VALUE_32_BYTES_!!"
).decode()
os.environ["GH_WEBHOOK_SECRET"] = "SYNTHETIC_TEST_VALUE"
os.environ["OPENAI_API_KEY"] = "SYNTHETIC_TEST_VALUE"
os.environ.pop("CLERK_JWT_KEY", None)

from app.config import settings


@pytest.fixture(scope="session")
def application_database_name() -> str | None:
    """Database name configured before test settings were sanitized."""
    return configured_database_name


@pytest.fixture(autouse=True)
def _disable_in_process_worker():
    """Keep route tests from starting the polling loop via lifespan."""
    original = settings.run_worker
    settings.run_worker = False
    yield
    settings.run_worker = original
