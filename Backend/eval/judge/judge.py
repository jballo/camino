"""Run the LLM-as-judge over a finished tour and reduce it to scores.

The judge is a single structured-output call: it sees the topic plus every step
in order and returns per-step faithfulness/relevance and tour-level
completeness/ordering (see ``schemas.py``). :func:`summarize` collapses that
verdict into mean per-dimension scores plus an overall, so the harness and CI
can compare runs and gate on a threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.models.tour import TourArtifact
from eval.judge.prompts import JUDGE_HUMAN, JUDGE_STEP, JUDGE_SYSTEM
from eval.judge.schemas import TourJudgment


def _format_steps(artifact: TourArtifact) -> str:
    blocks: list[str] = []
    for index, step in enumerate(artifact.steps):
        why = f"\nWhy: {step.why}" if step.why else ""
        blocks.append(
            JUDGE_STEP.format(
                index=index,
                title=step.title,
                file_path=step.file_path,
                start_line=step.start_line,
                end_line=step.end_line,
                explanation=step.explanation,
                why=why,
                snippet=step.snippet,
            )
        )
    return "\n\n".join(blocks)


async def judge_tour(llm: BaseChatModel, artifact: TourArtifact) -> TourJudgment:
    """Score ``artifact`` with a single structured-output judge call."""
    judge = llm.with_structured_output(TourJudgment)
    return await judge.ainvoke(
        [
            SystemMessage(content=JUDGE_SYSTEM),
            HumanMessage(
                content=JUDGE_HUMAN.format(
                    topic=artifact.topic,
                    repo_name=artifact.repo_name,
                    title=artifact.title,
                    step_count=len(artifact.steps),
                    steps=_format_steps(artifact),
                )
            ),
        ]
    )


@dataclass
class DimensionScores:
    """Reduced per-dimension scores for one judged tour.

    Per-step dimensions are averaged; tour-level dimensions pass through.
    ``overall`` is the mean of whichever dimensions are present, so a tour the
    judge scored only partially still yields a usable number.
    """

    faithfulness: float | None
    relevance: float | None
    completeness: int | None
    ordering: int | None
    overall: float | None
    scored_steps: int
    total_steps: int


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(judgment: TourJudgment, total_steps: int) -> DimensionScores:
    """Collapse a judgment into mean per-dimension scores plus an overall.

    Steps are aligned by position; extra step scores beyond ``total_steps`` are
    dropped so a model that returns too many entries can't skew the averages.
    """
    step_scores = judgment.steps[:total_steps]
    faithfulness = _mean([float(s.faithfulness) for s in step_scores])
    relevance = _mean([float(s.relevance) for s in step_scores])

    present = [
        float(v)
        for v in (faithfulness, relevance, judgment.completeness, judgment.ordering)
        if v is not None
    ]
    return DimensionScores(
        faithfulness=faithfulness,
        relevance=relevance,
        completeness=judgment.completeness,
        ordering=judgment.ordering,
        overall=_mean(present),
        scored_steps=len(step_scores),
        total_steps=total_steps,
    )
