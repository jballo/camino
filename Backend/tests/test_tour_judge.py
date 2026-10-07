"""No-LLM tests for the tour judge harness.

Covers the score reduction (:func:`eval.judge.summarize`), the judge call wiring
with a fake structured-output model, and the harness aggregation / threshold
logic. Nothing here touches OpenAI or Postgres.
"""

from __future__ import annotations

import pytest

from app.models.tour import TourArtifact, TourStep
from eval.judge import summarize
from eval.judge.judge import judge_tour
from eval.judge.schemas import StepScore, TourJudgment
from eval.run_tour_judge_eval import (
    JudgeRun,
    _aggregate,
    _run_from_scores,
)
from eval.judge.judge import DimensionScores
import time


def _artifact(n_steps: int = 2) -> TourArtifact:
    return TourArtifact(
        title="Sample tour",
        topic="how requests flow",
        repo_name="org/repo",
        steps=[
            TourStep(
                title=f"Step {i}",
                explanation="Explains the code.",
                file_path=f"src/mod{i}.py",
                start_line=1,
                end_line=2,
                snippet="def f():\n    return 1",
                why="Because.",
            )
            for i in range(n_steps)
        ],
    )


def _judgment(step_scores: list[tuple[int, int]], completeness: int, ordering: int) -> TourJudgment:
    return TourJudgment(
        completeness=completeness,
        ordering=ordering,
        steps=[
            StepScore(step_index=i, faithfulness=f, relevance=r)
            for i, (f, r) in enumerate(step_scores)
        ],
        summary="ok",
    )


class _FakeStructured:
    def __init__(self, result: TourJudgment):
        self._result = result

    async def ainvoke(self, _messages):
        return self._result


class _FakeLLM:
    """Stands in for ChatOpenAI: records the schema and returns a preset verdict."""

    def __init__(self, result: TourJudgment):
        self._result = result
        self.schema = None

    def with_structured_output(self, schema):
        self.schema = schema
        return _FakeStructured(self._result)


# --- summarize -------------------------------------------------------------


def test_summarize_averages_steps_and_passes_through_tour_dims():
    judgment = _judgment([(5, 4), (3, 2)], completeness=4, ordering=5)
    scores = summarize(judgment, total_steps=2)

    assert scores.faithfulness == pytest.approx(4.0)  # (5 + 3) / 2
    assert scores.relevance == pytest.approx(3.0)  # (4 + 2) / 2
    assert scores.completeness == 4
    assert scores.ordering == 5
    # overall = mean(4.0, 3.0, 4, 5)
    assert scores.overall == pytest.approx((4.0 + 3.0 + 4 + 5) / 4)
    assert scores.scored_steps == 2
    assert scores.total_steps == 2


def test_summarize_drops_extra_step_scores():
    # Model returned 3 step scores for a 2-step tour; the extra must be ignored
    # so it can't skew the per-step averages.
    judgment = _judgment([(5, 5), (5, 5), (1, 1)], completeness=5, ordering=5)
    scores = summarize(judgment, total_steps=2)

    assert scores.faithfulness == pytest.approx(5.0)
    assert scores.relevance == pytest.approx(5.0)
    assert scores.scored_steps == 2


def test_summarize_handles_no_step_scores():
    judgment = TourJudgment(completeness=3, ordering=4, steps=[], summary=None)
    scores = summarize(judgment, total_steps=0)

    assert scores.faithfulness is None
    assert scores.relevance is None
    # overall still derives from the tour-level dims that are present.
    assert scores.overall == pytest.approx(3.5)


# --- judge_tour wiring ------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_tour_uses_structured_output_and_returns_verdict():
    expected = _judgment([(5, 5), (4, 4)], completeness=4, ordering=4)
    llm = _FakeLLM(expected)

    result = await judge_tour(llm, _artifact(2))

    assert result is expected
    assert llm.schema is TourJudgment


# --- harness reduction ------------------------------------------------------


def _run_from(overall: float | None, min_score: float, error: str | None = None) -> JudgeRun:
    if error is not None:
        return JudgeRun(
            topic="t",
            title="",
            step_count=0,
            faithfulness=None,
            relevance=None,
            completeness=None,
            ordering=None,
            overall=None,
            passed=False,
            step_scores=[],
            summary=None,
            error=error,
            elapsed_s=0.0,
        )
    scores = DimensionScores(
        faithfulness=overall,
        relevance=overall,
        completeness=int(overall) if overall is not None else None,
        ordering=int(overall) if overall is not None else None,
        overall=overall,
        scored_steps=1,
        total_steps=1,
    )
    return _run_from_scores(_artifact(1), scores, "summary", [], min_score, time.monotonic())


def test_run_from_scores_threshold():
    assert _run_from(4.0, min_score=3.5).passed is True
    assert _run_from(3.5, min_score=3.5).passed is True
    assert _run_from(3.0, min_score=3.5).passed is False


def test_run_from_scores_none_overall_fails():
    scores = DimensionScores(None, None, None, None, None, 0, 0)
    run = _run_from_scores(_artifact(1), scores, None, [], 3.5, time.monotonic())
    assert run.passed is False


def test_aggregate_excludes_harness_errors_from_averages():
    runs = [
        _run_from(4.0, 3.5),
        _run_from(2.0, 3.5),
        _run_from(None, 3.5, error="judge failed: boom"),
    ]
    agg = _aggregate(runs)

    assert agg["topics"] == 3
    assert agg["harness_errors"] == 1
    assert agg["judged"] == 2
    assert agg["passed"] == 1
    # average is over the two judged runs only (4.0, 2.0), the error is excluded.
    assert agg["avg_overall"] == pytest.approx(3.0)


def test_aggregate_all_errors_yields_none_averages():
    runs = [_run_from(None, 3.5, error="generation failed: x")]
    agg = _aggregate(runs)

    assert agg["judged"] == 0
    assert agg["avg_overall"] is None
    assert agg["passed"] == 0
