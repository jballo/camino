"""Structured, user-facing issue brief artifact."""

from pydantic import BaseModel, Field

from app.models.tour import TourFreshness, TourStep


class BriefWarning(BaseModel):
    kind: str
    message: str = Field(min_length=1)
    url: str | None = None


class BriefConfidence(BaseModel):
    level: str
    issue_is_vague: bool = False
    questions_for_maintainer: list[str] = Field(default_factory=list)


class BriefSetupRecipe(BaseModel):
    steps: list[str] = Field(default_factory=list)
    target_branch: str | None = None
    target_branch_source: str
    target_branch_evidence: str | None = None
    target_branch_evidence_path: str | None = None
    default_branch: str | None = None
    fork_repo: str | None = None
    upstream_repo: str
    fork_commits_behind: int | None = Field(default=None, ge=0)
    fork_status_measurable: bool = False


class BriefArtifact(BaseModel):
    issue_title: str = Field(min_length=1)
    issue_number: int = Field(ge=1)
    repo_name: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    house_rules: list[str] = Field(default_factory=list)
    setup_recipe: BriefSetupRecipe
    reading_steps: list[TourStep] = Field(default_factory=list)
    test_guidance: list[str] = Field(default_factory=list)
    plan_checklist: list[str] = Field(default_factory=list)
    freshness: TourFreshness | None = None
    preflight_warnings: list[BriefWarning] = Field(default_factory=list)
    confidence: BriefConfidence
    honesty_note: str | None = None
