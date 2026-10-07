"""Deterministic tour-validation primitives shared by generation and eval.

These live in ``app`` (not ``eval``) so the production tour pipeline is
self-contained: importing ``app.tour`` must never depend on the ``eval`` package
being present on the path. ``eval.structural.validate`` re-exports these and adds
the disk/repo-clone checks that only the offline eval harness needs.

Grounding is by construction (snippets are extracted from stored chunk source in
``app.tour.extract``), so these checks are a safety net: a well-behaved Draft node
can't fail the structural checks, but they guard against future regressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Protocol

from app.models.tour import TourArtifact


class CheckKind(StrEnum):
    SCHEMA = "schema"
    PATH_EXISTS = "path_exists"
    LINES_IN_BOUNDS = "lines_in_bounds"
    SNIPPET_MATCHES = "snippet_matches"
    COVERAGE = "coverage"


class ChunkSource(Protocol):
    """The stored-source shape needed to validate a step without a disk clone.

    Satisfied by ``app.services.search.SearchResult`` and
    ``app.models.code.CodeChunkModel`` — anything carrying a file path, its
    absolute line range, and the exact source text for that range.
    """

    file_path: str
    start_line: int
    end_line: int
    source_code: str


@dataclass
class CheckIssue:
    kind: CheckKind
    message: str
    step_index: int | None = None


@dataclass
class ValidationResult:
    issues: list[CheckIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """A result passes iff it carries no issues.

        Derived rather than stored so the invariant can't drift — no caller can
        construct a ``passed=True`` result that also holds issues.
        """
        return not self.issues

    @property
    def failed_checks(self) -> set[CheckKind]:
        return {issue.kind for issue in self.issues}


def normalize_text(text: str) -> str:
    """Collapse insignificant whitespace so snippet checks tolerate formatting.

    Trailing whitespace and blank edge lines are dropped, but leading
    indentation on content lines is preserved — a bare ``.strip()`` would erase
    the first line's indentation and corrupt snippet matching for indented code.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip("\n")


def validate_tour_against_chunks(
    artifact: TourArtifact, chunks: Iterable[ChunkSource]
) -> ValidationResult:
    """Check every step against stored chunk source instead of a disk clone.

    The generation-time sibling of ``validate_tour_artifact``: at generation
    time the repo lives as chunks in Postgres, not as files on disk. Since
    snippets are extracted from ``CodeChunkModel.source_code`` (see
    ``app.tour.extract``), we validate each step against the *same stored source*:

    - ``PATH_EXISTS`` — the step's ``file_path`` was ingested (some chunk has it).
    - ``LINES_IN_BOUNDS`` — the step's line span sits inside an ingested chunk for
      that path.
    - ``SNIPPET_MATCHES`` — the snippet matches that chunk's source for the span.

    Mirrors the disk checks' one-issue-per-step / first-failure-wins semantics.
    """
    by_path: dict[str, list[ChunkSource]] = {}
    for chunk in chunks:
        by_path.setdefault(chunk.file_path, []).append(chunk)

    issues: list[CheckIssue] = []

    for step_index, step in enumerate(artifact.steps):
        path_chunks = by_path.get(step.file_path)
        if not path_chunks:
            issues.append(
                CheckIssue(
                    kind=CheckKind.PATH_EXISTS,
                    step_index=step_index,
                    message=f"no ingested chunk for file_path: {step.file_path!r}",
                )
            )
            continue

        covering = [
            c
            for c in path_chunks
            if c.start_line <= step.start_line and step.end_line <= c.end_line
        ]
        if not covering:
            issues.append(
                CheckIssue(
                    kind=CheckKind.LINES_IN_BOUNDS,
                    step_index=step_index,
                    message=(
                        f"lines {step.start_line}-{step.end_line} not within any "
                        f"ingested chunk for {step.file_path!r}"
                    ),
                )
            )
            continue

        normalized_snippet = normalize_text(step.snippet)
        if not normalized_snippet:
            issues.append(
                CheckIssue(
                    kind=CheckKind.SNIPPET_MATCHES,
                    step_index=step_index,
                    message=(
                        f"snippet is empty after normalization for "
                        f"{step.file_path!r}:{step.start_line}-{step.end_line}"
                    ),
                )
            )
            continue

        if not any(
            _chunk_slice_contains(chunk, step.start_line, step.end_line, normalized_snippet)
            for chunk in covering
        ):
            issues.append(
                CheckIssue(
                    kind=CheckKind.SNIPPET_MATCHES,
                    step_index=step_index,
                    message=(
                        f"snippet does not match ingested chunk source at "
                        f"{step.file_path!r}:{step.start_line}-{step.end_line}"
                    ),
                )
            )

    return ValidationResult(issues=issues)


def _chunk_slice_contains(
    chunk: ChunkSource, start_line: int, end_line: int, normalized_snippet: str
) -> bool:
    """True when the chunk's source for ``[start_line, end_line]`` holds the snippet."""
    lines = chunk.source_code.splitlines()
    rel_start = start_line - chunk.start_line
    rel_end = end_line - chunk.start_line
    if rel_start < 0 or rel_end >= len(lines) or rel_start > rel_end:
        return False
    slice_text = "\n".join(lines[rel_start : rel_end + 1])
    return normalized_snippet in normalize_text(slice_text)
