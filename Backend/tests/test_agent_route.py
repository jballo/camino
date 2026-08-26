import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import exc

from app.main import app
from app.db import get_session
from app.rate_limit import AGENT_ASK_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.agent.runner import AgentAnswer
from app.services.embeddings import EmbeddingError
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
    app.dependency_overrides[AGENT_ASK_RATE_LIMIT] = lambda: None
    yield
    app.dependency_overrides.clear()


client = TestClient(app)

ASK_URL = "/api/v1/agent/ask"

SAMPLE_SOURCE = SearchResult(
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

SAMPLE_ANSWER = AgentAnswer(answer="Login lives in auth.py", sources=[SAMPLE_SOURCE])


def _body(**overrides):
    base = {
        "question": "How does login work?",
        "repoName": "org/repo",
    }
    base.update(overrides)
    return base


# ── happy path ──────────────────────────────────────────────────────

@patch("app.api.agent.answer_question", new_callable=AsyncMock, return_value=SAMPLE_ANSWER)
def test_ask_returns_answer_and_sources(mock_answer):
    resp = client.post(ASK_URL, json=_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "Login lives in auth.py"
    assert len(data["sources"]) == 1
    assert data["sources"][0]["chunk_id"] == 10
    assert data["sources"][0]["score"] == 0.033


@patch("app.api.agent.answer_question", new_callable=AsyncMock, return_value=SAMPLE_ANSWER)
def test_ask_forwards_args_to_runner(mock_answer):
    resp = client.post(ASK_URL, json=_body())
    assert resp.status_code == 200
    _, kwargs = mock_answer.call_args
    assert kwargs["question"] == "How does login work?"
    assert kwargs["repo_name"] == "org/repo"
    assert kwargs["installation_id"] == FAKE_INSTALLATION_ID


@patch(
    "app.api.agent.answer_question",
    new_callable=AsyncMock,
    return_value=AgentAnswer(answer="No code found", sources=[]),
)
def test_ask_empty_sources(mock_answer):
    resp = client.post(ASK_URL, json=_body())
    assert resp.status_code == 200
    assert resp.json()["sources"] == []


@patch("app.api.agent.answer_question", new_callable=AsyncMock, return_value=SAMPLE_ANSWER)
def test_ask_source_has_expected_fields(mock_answer):
    resp = client.post(ASK_URL, json=_body())
    source = resp.json()["sources"][0]
    expected = {
        "chunk_id", "repo_name", "file_path", "symbol_name", "symbol_type",
        "language", "start_line", "end_line", "score",
    }
    assert set(source.keys()) == expected


# ── auth ────────────────────────────────────────────────────────────

@patch("app.api.agent.answer_question", new_callable=AsyncMock, return_value=SAMPLE_ANSWER)
def test_ask_rejects_deprecated_user_id_field(mock_answer):
    resp = client.post(ASK_URL, json=_body(userId="user_123"))
    assert resp.status_code == 422
    mock_answer.assert_not_called()


# ── validation ──────────────────────────────────────────────────────

def test_ask_missing_question_returns_422():
    resp = client.post(ASK_URL, json={"repoName": "org/repo"})
    assert resp.status_code == 422


def test_ask_empty_question_returns_422():
    resp = client.post(ASK_URL, json=_body(question=""))
    assert resp.status_code == 422


def test_ask_question_too_long_returns_422():
    resp = client.post(ASK_URL, json=_body(question="x" * 4001))
    assert resp.status_code == 422


def test_ask_missing_repo_returns_422():
    resp = client.post(ASK_URL, json={"question": "q"})
    assert resp.status_code == 422


# ── github connection lookup ────────────────────────────────────────

def test_ask_no_github_connection_returns_404():
    def _no_conn_session():
        session = MagicMock()
        session.exec.return_value.one.side_effect = exc.NoResultFound()
        yield session

    app.dependency_overrides[get_session] = _no_conn_session
    resp = client.post(ASK_URL, json=_body())
    assert resp.status_code == 404


# ── runner failure modes ────────────────────────────────────────────

@patch(
    "app.api.agent.answer_question",
    new_callable=AsyncMock,
    side_effect=EmbeddingError("down"),
)
def test_ask_embedding_error_returns_502(mock_answer):
    resp = client.post(ASK_URL, json=_body())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Embedding service unavailable"


@patch(
    "app.api.agent.answer_question",
    new_callable=AsyncMock,
    side_effect=exc.SQLAlchemyError("boom"),
)
def test_ask_database_error_returns_502(mock_answer):
    resp = client.post(ASK_URL, json=_body())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Database service error"


@patch(
    "app.api.agent.answer_question",
    new_callable=AsyncMock,
    side_effect=RuntimeError("unexpected"),
)
def test_ask_unexpected_error_returns_500(mock_answer):
    resp = client.post(ASK_URL, json=_body())
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal agent error"
