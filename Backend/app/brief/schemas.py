"""Internal structured-output schemas for issue brief generation."""

from pydantic import BaseModel, Field


class BriefSynthesis(BaseModel):
    scope_summary: str = Field(min_length=1)
    retrieval_queries: list[str] = Field(min_length=1, max_length=6)
    issue_keywords: list[str] = Field(default_factory=list, max_length=20)
    issue_is_vague: bool = False
    confidence: str = Field(description="One of: high, medium, low")
    questions_for_maintainer: list[str] = Field(default_factory=list, max_length=8)


class BriefDraft(BaseModel):
    summary: str = Field(min_length=1)
    house_rules: list[str] = Field(default_factory=list, max_length=10)
    setup_steps: list[str] = Field(default_factory=list, max_length=10)
    test_guidance: list[str] = Field(default_factory=list, max_length=10)
    plan_checklist: list[str] = Field(default_factory=list, max_length=12)


class BriefReadingDraft(BaseModel):
    chunk_id: int
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    why: str | None = None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
