import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.tour import TourArtifact, TourStep
from app.services.search import SearchResult
from app.tour.extract import _clamp_span, build_grounded_step
from app.tour.graph import _format_candidates, _pick_chunk
from app.tour.review import coverage_issues, review_tour
from app.tour.runner import TourGenerationError, generate_tour
from app.tour.schemas import DraftedStep, PlannedStep, TourPlan
from eval.structural.validate import CheckKind


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


# ── coverage checks ─────────────────────────────────────────────────

def _step(file_path: str, start: int, end: int) -> TourStep:
    return TourStep(
        title="t",
        explanation="e",
        file_path=file_path,
        start_line=start,
        end_line=end,
        snippet="code",
    )


def _artifact(*steps: TourStep) -> TourArtifact:
    return TourArtifact(title="T", topic="x", repo_name="org/repo", steps=list(steps))


def test_coverage_clean_tour_has_no_issues():
    artifact = _artifact(_step("a.py", 1, 2), _step("b.py", 1, 2))
    assert coverage_issues(artifact, planned_count=2, min_distinct_files=2) == []


def test_coverage_flags_missing_steps():
    artifact = _artifact(_step("a.py", 1, 2))
    issues = coverage_issues(artifact, planned_count=3, min_distinct_files=1)
    assert any(i.kind == CheckKind.COVERAGE and "planned" in i.message for i in issues)


def test_coverage_flags_too_few_distinct_files():
    artifact = _artifact(_step("a.py", 1, 2), _step("a.py", 5, 6))
    issues = coverage_issues(artifact, planned_count=2, min_distinct_files=2)
    assert any(i.kind == CheckKind.COVERAGE and "distinct file" in i.message for i in issues)


def test_coverage_flags_duplicate_citation_with_step_index():
    artifact = _artifact(_step("a.py", 1, 2), _step("a.py", 1, 2))
    issues = coverage_issues(artifact, planned_count=2, min_distinct_files=1)
    dupes = [i for i in issues if "duplicate" in i.message]
    assert len(dupes) == 1
    assert dupes[0].step_index == 1


def test_coverage_min_files_clamped_to_plan_length():
    # A legitimately short (1-step) tour must not be failed for spanning 1 file.
    artifact = _artifact(_step("a.py", 1, 2))
    assert coverage_issues(artifact, planned_count=1, min_distinct_files=2) == []


def test_review_tour_combines_structural_and_coverage():
    # Citation is fine structurally, but the single step spans one file.
    chunk = _result(1, file_path="a.py", start_line=1, end_line=4)
    step = build_grounded_step(
        chunk=chunk, title="t", explanation="e", why=None, req_start=1, req_end=4
    )
    artifact = _artifact(step, step)  # duplicate -> coverage issue, structural ok
    result = review_tour(artifact, [chunk], planned_count=2, min_distinct_files=2)
    assert not result.passed
    assert result.failed_checks == {CheckKind.COVERAGE}


# ── repair loop (Draft <-> Review) ──────────────────────────────────

class _SeqLLM:
    """Fake LLM: fixed plan, and a sequence of drafts returned per Draft call."""

    def __init__(self, plan: TourPlan, drafts: list[DraftedStep]):
        self._plan = plan
        self._drafts = drafts
        self.draft_calls = 0

    def with_structured_output(self, model):
        if model is TourPlan:
            return _Structured(self._plan)
        if model is DraftedStep:
            return self
        raise AssertionError(f"unexpected structured-output model: {model}")

    async def ainvoke(self, _messages):
        i = min(self.draft_calls, len(self._drafts) - 1)
        self.draft_calls += 1
        return self._drafts[i]


def _two_step_plan() -> TourPlan:
    return TourPlan(
        title="Two",
        steps=[
            PlannedStep(step_intent="first", search_query="q1"),
            PlannedStep(step_intent="second", search_query="q2"),
        ],
    )


@pytest.mark.asyncio
@patch("app.tour.graph.hybrid_search", new_callable=AsyncMock)
@patch("app.tour.runner.ChatOpenAI")
async def test_repair_loop_fixes_duplicate_citation(mock_chat, mock_search):
    pool = [
        _result(1, file_path="a.py", start_line=10, end_line=13),
        _result(2, file_path="b.py", start_line=20, end_line=23),
    ]
    mock_search.return_value = pool

    # Both steps first pick chunk 1 (duplicate); the repair pass moves step 2 to
    # chunk 2, giving two distinct files.
    drafts = [
        DraftedStep(chunk_id=1, title="t", explanation="e", start_line=10, end_line=13),
        DraftedStep(chunk_id=1, title="t", explanation="e", start_line=10, end_line=13),
        DraftedStep(chunk_id=2, title="t", explanation="e", start_line=20, end_line=23),
    ]
    llm = _SeqLLM(_two_step_plan(), drafts)
    mock_chat.return_value = llm

    artifact = await generate_tour(
        MagicMock(), topic="topic", repo_name="org/repo", installation_id=1
    )

    assert llm.draft_calls == 3  # 2 (first pass) + 1 (repair of step 2)
    assert {s.file_path for s in artifact.steps} == {"a.py", "b.py"}


@pytest.mark.asyncio
@patch("app.tour.graph.hybrid_search", new_callable=AsyncMock)
@patch("app.tour.runner.ChatOpenAI")
async def test_repair_loop_stops_when_issue_is_unrepairable(mock_chat, mock_search):
    # Two distinct citations but same file: a coverage issue with no step to
    # redraft, so the loop must not spin — no repair pass should run.
    pool = [_result(1, file_path="a.py", start_line=10, end_line=13)]
    mock_search.return_value = pool

    drafts = [
        DraftedStep(chunk_id=1, title="t", explanation="e", start_line=10, end_line=11),
        DraftedStep(chunk_id=1, title="t", explanation="e", start_line=12, end_line=13),
    ]
    llm = _SeqLLM(_two_step_plan(), drafts)
    mock_chat.return_value = llm

    artifact = await generate_tour(
        MagicMock(), topic="topic", repo_name="org/repo", installation_id=1
    )

    assert llm.draft_calls == 2  # only the first pass; no unproductive repair
    assert len(artifact.steps) == 2
