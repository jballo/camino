"""LLM structured-output schemas for the tour generation pipeline.

These are the *internal* shapes the model fills in at each node. The user-facing
contract stays :class:`app.models.tour.TourArtifact` / ``TourStep`` — a drafted
step is turned into a grounded ``TourStep`` by deterministic extraction (see
``app.tour.extract``), never by trusting model-authored citation fields.
"""

from pydantic import BaseModel, Field


class PlannedStep(BaseModel):
    """One item in the tour outline produced by the Plan node."""

    step_intent: str = Field(
        min_length=1,
        description=(
            "What a newcomer should understand after this step — the concept or "
            "behaviour to explain (not the code itself)."
        ),
    )
    search_query: str = Field(
        min_length=1,
        description=(
            "A focused natural-language query used to retrieve the code this step "
            "should be grounded in (e.g. 'where JWT sessions are validated')."
        ),
    )


class TourPlan(BaseModel):
    """Ordered outline for a guided tour."""

    title: str = Field(min_length=1, description="Concise, specific tour title.")
    steps: list[PlannedStep] = Field(
        min_length=1,
        description="Ordered steps forming a logical narrative from entry point to detail.",
    )


class DraftedStep(BaseModel):
    """A single tour step authored by the Draft node.

    The model owns only the prose (``title``/``explanation``/``why``) and the
    *selection* of which retrieved chunk to cite plus the tightest relevant line
    span within it. The snippet and final citation fields are derived
    deterministically from the chosen chunk's stored source.
    """

    chunk_id: int = Field(
        description="The `chunk_id` of the chosen candidate to cite for this step."
    )
    title: str = Field(min_length=1, description="Short, descriptive step title.")
    explanation: str = Field(
        min_length=1,
        description="What the cited code does, grounded strictly in the chosen snippet.",
    )
    why: str | None = Field(
        default=None,
        description="Optional: why this code exists / why it matters for the topic.",
    )
    start_line: int = Field(
        ge=1,
        description="Absolute start line (in the file) of the tightest relevant span.",
    )
    end_line: int = Field(
        ge=1,
        description="Absolute end line (in the file) of the tightest relevant span.",
    )
