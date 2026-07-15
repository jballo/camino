import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.db import get_session
from app.rate_limit import REPOSITORY_SEARCH_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.search import SearchResult


def _noop_verify():
    return "user_123"


FAKE_INSTALLATION_ID = 12345


def _fake_session():
    session = MagicMock()
    gh_conn = MagicMock()
    gh_conn.installationId = FAKE_INSTALLATION_ID
    session.exec.return_value.one.return_value = gh_conn
    yield session


@pytest.fixture(autouse=True)
def _override_deps():
    app.dependency_overrides[get_authenticated_user_id] = _noop_verify
    app.dependency_overrides[get_session] = _fake_session
    app.dependency_overrides[REPOSITORY_SEARCH_RATE_LIMIT] = lambda: None
    yield
    app.dependency_overrides.clear()


client = TestClient(app)

SEARCH_URL = "/api/v1/repositories/search"

SAMPLE_RESULT = SearchResult(
    chunk_id=10,
    repo_name="org/repo",
    file_path="src/auth.py",
    symbol_name="login",
    symbol_type="function",
    language="py",
    start_line=1,
    end_line=10,
    source_code="def login(): ...",
    signature="def login():",
    docstring="Handles login.",
    score=0.033,
)


@patch(
    "app.api.repositories.hybrid_search",
    new_callable=AsyncMock,
    return_value=[SAMPLE_RESULT],
)
def test_search_returns_results(mock_search):
    resp = client.post(SEARCH_URL, json={
        "query": "login",
        "repoName": "org/repo",
        "userId": "user_123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["chunk_id"] == 10
    assert data[0]["file_path"] == "src/auth.py"
    assert data[0]["score"] == 0.033


@patch(
    "app.api.repositories.hybrid_search",
    new_callable=AsyncMock,
    return_value=[SAMPLE_RESULT],
)
def test_search_passes_limit(mock_search):
    resp = client.post(SEARCH_URL, json={
        "query": "login",
        "repoName": "org/repo",
        "userId": "user_123",
        "limit": 5,
    })
    assert resp.status_code == 200
    _, kwargs = mock_search.call_args
    assert kwargs["limit"] == 5


@patch(
    "app.api.repositories.hybrid_search",
    new_callable=AsyncMock,
    return_value=[],
)
def test_search_empty_results(mock_search):
    resp = client.post(SEARCH_URL, json={
        "query": "nonexistent",
        "repoName": "org/repo",
        "userId": "user_123",
    })
    assert resp.status_code == 200
    assert resp.json() == []


def test_search_missing_query_returns_422():
    resp = client.post(SEARCH_URL, json={
        "repoName": "org/repo",
        "userId": "user_123",
    })
    assert resp.status_code == 422


def test_search_missing_repo_returns_422():
    resp = client.post(SEARCH_URL, json={
        "query": "login",
        "userId": "user_123",
    })
    assert resp.status_code == 422


def test_search_missing_userId_returns_422():
    resp = client.post(SEARCH_URL, json={
        "query": "login",
        "repoName": "org/repo",
    })
    assert resp.status_code == 422


@patch(
    "app.api.repositories.hybrid_search",
    new_callable=AsyncMock,
    return_value=[SAMPLE_RESULT],
)
def test_search_default_limit_is_10(mock_search):
    resp = client.post(SEARCH_URL, json={
        "query": "login",
        "repoName": "org/repo",
        "userId": "user_123",
    })
    assert resp.status_code == 200
    _, kwargs = mock_search.call_args
    assert kwargs["limit"] == 10


@patch(
    "app.api.repositories.hybrid_search",
    new_callable=AsyncMock,
    return_value=[SAMPLE_RESULT],
)
def test_search_response_has_all_fields(mock_search):
    resp = client.post(SEARCH_URL, json={
        "query": "login",
        "repoName": "org/repo",
        "userId": "user_123",
    })
    data = resp.json()[0]
    expected_fields = {
        "chunk_id", "repo_name", "file_path", "symbol_name", "symbol_type",
        "language", "start_line", "end_line", "source_code", "signature",
        "docstring", "score",
    }
    assert set(data.keys()) == expected_fields
