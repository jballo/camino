import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.tour import TourArtifact
from app.services.search import SearchResult
from app.tour.extract import _clamp_span, build_grounded_step
from app.tour.graph import _format_candidates, _pick_chunk
from app.tour.runner import TourGenerationError, generate_tour
from app.tour.schemas import DraftedStep, PlannedStep, TourPlan


def _result(chunk_id: int, **overrides) -> SearchResult:
    base = dict(
        chunk_id=chunk_id,
        repo_name="org/repo",
        file_path="src/auth.py",
        symbol_name="login",
        symbol_type="function",
        language="py",
        start_line=10,
        end_line=13,
        source_code="def login():\n    validate()\n    issue_token()\n    return ok",
        signature="def login():",
        docstring="Handles login.",
        score=0.033,
    )
    base.update(overrides)
    return SearchResult(**base)


# ── _clamp_span (pure logic) ────────────────────────────────────────

def test_clamp_span_maps_absolute_to_relative():
    chunk = _result(1)  # lines 10..13
    assert _clamp_span(chunk, 11, 12) == (1, 2)


def test_clamp_span_full_chunk():
    chunk = _result(1)
    assert _clamp_span(chunk, 10, 13) == (0, 3)


def test_clamp_span_out_of_range_falls_back_to_whole_chunk():
    chunk = _result(1)  # 4 source lines
    assert _clamp_span(chunk, 100, 200) == (0, 3)


def test_clamp_span_inverted_falls_back_to_whole_chunk():
    chunk = _result(1)
    assert _clamp_span(chunk, 12, 11) == (0, 3)


def test_clamp_span_empty_source():
    chunk = _result(1, source_code="")
    assert _clamp_span(chunk, 10, 10) == (0, 0)


# ── build_grounded_step (grounding by construction) ─────────────────

def test_build_grounded_step_extracts_exact_snippet():
    chunk = _result(1)
    step = build_grounded_step(
        chunk=chunk,
        title="Login entry",
        explanation="Validates then issues a token.",
        why="It's the auth entry point.",
        req_start=11,
        req_end=12,
    )
    assert step.file_path == "src/auth.py"
    assert step.start_line == 11
    assert step.end_line == 12
    assert step.snippet == "    validate()\n    issue_token()"
    assert step.why == "It's the auth entry point."


def test_build_grounded_step_snippet_is_substring_of_source():
    chunk = _result(1)
    step = build_grounded_step(
        chunk=chunk,
        title="t",
        explanation="e",
        why=None,
        req_start=10,
        req_end=13,
    )
    assert step.snippet in chunk.source_code
    assert step.snippet == chunk.source_code


def test_build_grounded_step_out_of_range_uses_whole_chunk():
    chunk = _result(1)
    step = build_grounded_step(
        chunk=chunk,
        title="t",
        explanation="e",
        why=None,
        req_start=999,
        req_end=1000,
    )
    assert step.start_line == 10
    assert step.end_line == 13
    assert step.snippet == chunk.source_code


def test_build_grounded_step_empty_why_becomes_none():
    step = build_grounded_step(
        chunk=_result(1),
        title="t",
        explanation="e",
        why="",
        req_start=11,
        req_end=11,
    )
    assert step.why is None


# ── _pick_chunk ─────────────────────────────────────────────────────

def test_pick_chunk_returns_matching_id():
    cands = [_result(10), _result(20), _result(30)]
    assert _pick_chunk(cands, 20).chunk_id == 20


def test_pick_chunk_unknown_id_defaults_to_first():
    cands = [_result(10), _result(20)]
    assert _pick_chunk(cands, 999).chunk_id == 10


# ── _format_candidates ──────────────────────────────────────────────

def test_format_candidates_includes_id_path_and_numbered_source():
    out = _format_candidates([_result(42)])
    assert "chunk_id=42" in out
    assert "src/auth.py:10-13" in out
    assert "    10 | def login():" in out
    assert "    11 |     validate()" in out


def test_format_candidates_truncates_long_source():
    long_source = "\n".join(f"line {i}" for i in range(2000))
    out = _format_candidates([_result(1, source_code=long_source, end_line=2010)])
    assert "… (truncated)" in out


# ── generate_tour (orchestration) ───────────────────────────────────

class _Structured:
    """Fake `.with_structured_output(...)` handle returning a fixed value."""

    def __init__(self, value):
        self._value = value

    async def ainvoke(self, _messages):
        return self._value


class _FakeLLM:
    def __init__(self, plan: TourPlan, draft: DraftedStep):
        self._plan = plan
        self._draft = draft

    def with_structured_output(self, model):
        if model is TourPlan:
            return _Structured(self._plan)
        if model is DraftedStep:
            return _Structured(self._draft)
        raise AssertionError(f"unexpected structured-output model: {model}")


@pytest.mark.asyncio
@patch("app.tour.graph.hybrid_search", new_callable=AsyncMock)
@patch("app.tour.runner.ChatOpenAI")
async def test_generate_tour_builds_grounded_artifact(mock_chat, mock_search):
    plan = TourPlan(
        title="Auth flow",
        steps=[PlannedStep(step_intent="how login works", search_query="login validate token")],
    )
    draft = DraftedStep(
        chunk_id=10,
        title="Login entry point",
        explanation="Validates and issues a token.",
        why="Auth entry point.",
        start_line=11,
        end_line=12,
    )
    mock_chat.return_value = _FakeLLM(plan, draft)
    mock_search.return_value = [_result(10)]

    artifact = await generate_tour(
        MagicMock(),
        topic="authentication",
        repo_name="org/repo",
        installation_id=1,
    )

    assert isinstance(artifact, TourArtifact)
    assert artifact.title == "Auth flow"
    assert artifact.topic == "authentication"
    assert len(artifact.steps) == 1
    step = artifact.steps[0]
    assert step.file_path == "src/auth.py"
    assert step.snippet == "    validate()\n    issue_token()"


@pytest.mark.asyncio
@patch("app.tour.graph.hybrid_search", new_callable=AsyncMock)
@patch("app.tour.runner.ChatOpenAI")
async def test_generate_tour_raises_when_no_candidates(mock_chat, mock_search):
    plan = TourPlan(
        title="Empty",
        steps=[PlannedStep(step_intent="x", search_query="y")],
    )
    draft = DraftedStep(chunk_id=1, title="t", explanation="e", start_line=1, end_line=1)
    mock_chat.return_value = _FakeLLM(plan, draft)
    mock_search.return_value = []  # nothing retrieved -> no grounded steps

    with pytest.raises(TourGenerationError):
        await generate_tour(
            MagicMock(),
            topic="topic",
            repo_name="org/repo",
            installation_id=1,
        )
