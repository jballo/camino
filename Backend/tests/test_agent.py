import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from app.agent.runner import _dedupe_sources, answer_question, AgentAnswer
from app.agent.tools import (
    MAX_SOURCE_CHARS,
    _format_results,
    build_hybrid_search_tool,
)
from app.services.search import SearchResult


def _result(chunk_id: int, **overrides) -> SearchResult:
    base = dict(
        chunk_id=chunk_id,
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
    base.update(overrides)
    return SearchResult(**base)


# ── _dedupe_sources (pure logic) ────────────────────────────────────

def test_dedupe_sources_removes_duplicate_chunk_ids():
    sources = [_result(10), _result(20), _result(10)]
    result = _dedupe_sources(sources)
    assert [s.chunk_id for s in result] == [10, 20]


def test_dedupe_sources_preserves_first_occurrence_order():
    sources = [_result(30), _result(10), _result(20), _result(10)]
    result = _dedupe_sources(sources)
    assert [s.chunk_id for s in result] == [30, 10, 20]


def test_dedupe_sources_empty():
    assert _dedupe_sources([]) == []


def test_dedupe_sources_no_duplicates():
    sources = [_result(1), _result(2), _result(3)]
    result = _dedupe_sources(sources)
    assert [s.chunk_id for s in result] == [1, 2, 3]


# ── _format_results (pure logic) ────────────────────────────────────

def test_format_results_empty_returns_guidance():
    out = _format_results([])
    assert "No relevant code was found" in out


def test_format_results_includes_header_and_code():
    out = _format_results([_result(10)])
    assert "[1] src/auth.py:1-10 (function login)" in out
    assert "```py" in out
    assert "def login(): ..." in out


def test_format_results_numbers_multiple_blocks():
    out = _format_results([_result(10), _result(20, symbol_name="logout")])
    assert "[1] " in out
    assert "[2] " in out


def test_format_results_truncates_long_source():
    long_body = "x" * (MAX_SOURCE_CHARS + 500)
    out = _format_results([_result(10, source_code=long_body)])
    assert "# ... (truncated)" in out
    # only MAX_SOURCE_CHARS of the body should survive
    assert out.count("x") == MAX_SOURCE_CHARS


def test_format_results_does_not_truncate_short_source():
    out = _format_results([_result(10, source_code="short")])
    assert "# ... (truncated)" not in out


# ── build_hybrid_search_tool ────────────────────────────────────────

def test_build_hybrid_search_tool_metadata():
    sink: list[SearchResult] = []
    tool = build_hybrid_search_tool(MagicMock(), "org/repo", 1, sink=sink)
    assert tool.name == "hybrid_search"
    assert "org/repo" in tool.description


@pytest.mark.asyncio
@patch("app.agent.tools.hybrid_search", new_callable=AsyncMock)
async def test_hybrid_search_tool_appends_to_sink(mock_search):
    mock_search.return_value = [_result(10), _result(20)]
    sink: list[SearchResult] = []
    session = MagicMock()
    tool = build_hybrid_search_tool(session, "org/repo", 99, sink=sink, limit=5)

    out = await tool.ainvoke({"query": "login"})

    assert [s.chunk_id for s in sink] == [10, 20]
    assert "[1] src/auth.py" in out
    _, kwargs = mock_search.call_args
    assert kwargs["installation_id"] == 99
    assert kwargs["limit"] == 5


@pytest.mark.asyncio
@patch("app.agent.tools.hybrid_search", new_callable=AsyncMock)
async def test_hybrid_search_tool_empty_results(mock_search):
    mock_search.return_value = []
    sink: list[SearchResult] = []
    tool = build_hybrid_search_tool(MagicMock(), "org/repo", 1, sink=sink)

    out = await tool.ainvoke({"query": "nope"})

    assert sink == []
    assert "No relevant code was found" in out


@pytest.mark.asyncio
@patch("app.agent.tools.hybrid_search", new_callable=AsyncMock)
async def test_hybrid_search_tool_accumulates_across_calls(mock_search):
    sink: list[SearchResult] = []
    tool = build_hybrid_search_tool(MagicMock(), "org/repo", 1, sink=sink)

    mock_search.return_value = [_result(10)]
    await tool.ainvoke({"query": "first"})
    mock_search.return_value = [_result(20)]
    await tool.ainvoke({"query": "second"})

    assert [s.chunk_id for s in sink] == [10, 20]


# ── answer_question (orchestration) ─────────────────────────────────

@pytest.mark.asyncio
@patch("app.agent.runner.build_agent")
@patch("app.agent.runner.ChatOpenAI")
@patch("app.agent.runner.build_hybrid_search_tool")
async def test_answer_question_returns_final_message(
    mock_build_tool, mock_chat, mock_build_agent
):
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="The login flow lives in auth.py")]}
    )
    mock_build_agent.return_value = mock_agent

    result = await answer_question(
        MagicMock(),
        question="How does login work?",
        repo_name="org/repo",
        installation_id=1,
    )

    assert isinstance(result, AgentAnswer)
    assert result.answer == "The login flow lives in auth.py"


@pytest.mark.asyncio
@patch("app.agent.runner.build_agent")
@patch("app.agent.runner.ChatOpenAI")
@patch("app.agent.runner.build_hybrid_search_tool")
async def test_answer_question_dedupes_sink_sources(
    mock_build_tool, mock_chat, mock_build_agent
):
    # Simulate the tool populating the sink (the list passed via sink=)
    def _capture_tool(session, repo, inst, *, sink, limit):
        sink.extend([_result(10), _result(10), _result(20)])
        return MagicMock()

    mock_build_tool.side_effect = _capture_tool

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="answer")]}
    )
    mock_build_agent.return_value = mock_agent

    result = await answer_question(
        MagicMock(),
        question="q",
        repo_name="org/repo",
        installation_id=1,
    )

    assert [s.chunk_id for s in result.sources] == [10, 20]


@pytest.mark.asyncio
@patch("app.agent.runner.build_agent")
@patch("app.agent.runner.ChatOpenAI")
@patch("app.agent.runner.build_hybrid_search_tool")
async def test_answer_question_stringifies_non_string_content(
    mock_build_tool, mock_chat, mock_build_agent
):
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content=[{"type": "text", "text": "hi"}])]}
    )
    mock_build_agent.return_value = mock_agent

    result = await answer_question(
        MagicMock(),
        question="q",
        repo_name="org/repo",
        installation_id=1,
    )

    assert isinstance(result.answer, str)


@pytest.mark.asyncio
@patch("app.agent.runner.build_agent")
@patch("app.agent.runner.ChatOpenAI")
@patch("app.agent.runner.build_hybrid_search_tool")
async def test_answer_question_uses_custom_model(
    mock_build_tool, mock_chat, mock_build_agent
):
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="ok")]}
    )
    mock_build_agent.return_value = mock_agent

    await answer_question(
        MagicMock(),
        question="q",
        repo_name="org/repo",
        installation_id=1,
        model="gpt-custom",
    )

    _, kwargs = mock_chat.call_args
    assert kwargs["model"] == "gpt-custom"
