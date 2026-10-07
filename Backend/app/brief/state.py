from typing import TypedDict

from app.brief.schemas import BriefDraft, BriefSynthesis
from app.models.tour import TourStep
from app.services.fork_status import ForkStatus
from app.services.issue_thread import IssueThread
from app.services.search import SearchResult
from app.services.staleness import HeadComparison
from app.services.target_branch import TargetBranchResolution


class BriefState(TypedDict, total=False):
    repo_name: str
    ref: str
    installation_id: int
    issue: IssueThread
    target_resolution: TargetBranchResolution
    fork_status: ForkStatus
    allow_stale: bool
    synthesis: BriefSynthesis
    candidates: dict[int, list[SearchResult]]
    comparison: HeadComparison
    draft: BriefDraft
    reading_drafts: dict[int, TourStep]
    reading_steps: list[TourStep]
    review_issues: list[str]
    repair_indices: list[int]
    attempts: int
    honesty_note: str | None
