"""Review node helpers — the safety net for tour generation.

Because snippets are extracted from stored chunk source (see
``app.tour.extract``), the structural checks (``PATH_EXISTS`` /
``LINES_IN_BOUNDS`` / ``SNIPPET_MATCHES``) cannot fail for a well-behaved
Draft node — they run here as a guard against future regressions. The checks
that actually gate the repair loop are the coverage ones: did we produce every
planned step, span enough of the codebase, and avoid citing the same lines
twice?
"""

from __future__ import annotations

from app.models.tour import TourArtifact
from app.services.search import SearchResult
from eval.structural.validate import (
    CheckIssue,
    CheckKind,
    ValidationResult,
    validate_tour_against_chunks,
)

DEFAULT_MIN_DISTINCT_FILES = 2


def coverage_issues(
    artifact: TourArtifact,
    *,
    planned_count: int,
    min_distinct_files: int = DEFAULT_MIN_DISTINCT_FILES,
) -> list[CheckIssue]:
    """Cheap, no-LLM coverage checks over a drafted tour.

    ``min_distinct_files`` is clamped to the number of planned steps so a
    legitimately short tour (e.g. a 1-step plan) isn't failed for spanning too
    few files.
    """
    issues: list[CheckIssue] = []

    produced = len(artifact.steps)
    if produced < planned_count:
        issues.append(
            CheckIssue(
                kind=CheckKind.COVERAGE,
                message=(
                    f"only {produced}/{planned_count} planned steps produced a "
                    "grounded step"
                ),
            )
        )

    effective_min = min(min_distinct_files, planned_count)
    distinct_files = {step.file_path for step in artifact.steps}
    if len(distinct_files) < effective_min:
        issues.append(
            CheckIssue(
                kind=CheckKind.COVERAGE,
                message=(
                    f"steps span only {len(distinct_files)} distinct file(s); "
                    f"expected >= {effective_min}"
                ),
            )
        )

    seen: dict[tuple[str, int, int], int] = {}
    for step_index, step in enumerate(artifact.steps):
        key = (step.file_path, step.start_line, step.end_line)
        first = seen.get(key)
        if first is not None:
            issues.append(
                CheckIssue(
                    kind=CheckKind.COVERAGE,
                    step_index=step_index,
                    message=(
                        f"duplicate citation {step.file_path}:{step.start_line}-"
                        f"{step.end_line} (already cited by step {first})"
                    ),
                )
            )
        else:
            seen[key] = step_index

    return issues


def review_tour(
    artifact: TourArtifact,
    chunks: list[SearchResult],
    *,
    planned_count: int,
    min_distinct_files: int = DEFAULT_MIN_DISTINCT_FILES,
) -> ValidationResult:
    """Combine structural grounding checks with coverage checks.

    ``chunks`` is the flattened candidate pool retrieved for the tour; the
    structural checks validate each step's citation against that stored source
    rather than a disk clone (§5 option A).
    """
    structural = validate_tour_against_chunks(artifact, chunks)
    coverage = coverage_issues(
        artifact,
        planned_count=planned_count,
        min_distinct_files=min_distinct_files,
    )
    return ValidationResult(issues=structural.issues + coverage)
