"""Structured-output schemas for the LLM-as-judge tour eval.

The judge scores a finished :class:`app.models.tour.TourArtifact` on four
qualitative dimensions the deterministic structural eval cannot see:

- **faithfulness** (per step): does the explanation stay true to the cited
  snippet, with no claims the code doesn't support?
- **relevance** (per step): does the step meaningfully serve the tour topic?
- **completeness** (per tour): do the steps together cover the topic's
  important aspects?
- **ordering** (per tour): do the steps flow in a sensible teaching order?

One judge call scores a whole tour: the model sees every step in order, so it
can reason about completeness and ordering with full context. Per-step scores
come back as an ordered list aligned with ``TourArtifact.steps``.
"""

from pydantic import BaseModel, Field

# 1-5 anchors are documented in the judge prompt; the schema only enforces the
# range so a malformed score can't leak into the aggregates.
MIN_SCORE = 1
MAX_SCORE = 5


class StepScore(BaseModel):
    """The judge's per-step verdict, one entry per tour step in order."""

    step_index: int = Field(
        ge=0,
        description="0-based index of the step being scored, matching input order.",
    )
    faithfulness: int = Field(
        ge=MIN_SCORE,
        le=MAX_SCORE,
        description=(
            "1-5: how well the explanation is supported by the cited snippet "
            "(5 = fully grounded, 1 = contradicts or invents behaviour)."
        ),
    )
    relevance: int = Field(
        ge=MIN_SCORE,
        le=MAX_SCORE,
        description=(
            "1-5: how much this step serves the tour topic "
            "(5 = central, 1 = unrelated)."
        ),
    )
    notes: str | None = Field(
        default=None,
        description="Optional short justification, especially for low scores.",
    )


class TourJudgment(BaseModel):
    """The judge's full verdict on one tour."""

    completeness: int = Field(
        ge=MIN_SCORE,
        le=MAX_SCORE,
        description=(
            "1-5: how completely the steps together cover the topic "
            "(5 = comprehensive, 1 = major gaps)."
        ),
    )
    ordering: int = Field(
        ge=MIN_SCORE,
        le=MAX_SCORE,
        description=(
            "1-5: how logically the steps flow as a narrative "
            "(5 = ideal progression, 1 = incoherent order)."
        ),
    )
    steps: list[StepScore] = Field(
        description="Per-step scores, one per input step, in the same order.",
    )
    summary: str | None = Field(
        default=None,
        description="Optional one-or-two sentence overall assessment of the tour.",
    )
