"""Validate tour artifacts against schema and on-disk repo source.

The shared primitives (``CheckKind`` / ``CheckIssue`` / ``ValidationResult`` /
``normalize_text`` / ``validate_tour_against_chunks``) now live in
``app.tour.checks`` so the production pipeline never depends on ``eval`` being on
the path; they're re-exported here for backwards compatibility. This module keeps
the *disk/repo-clone* checks that only the offline eval harness needs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.tour import TourArtifact
from app.tour.checks import (
    CheckIssue,
    CheckKind,
    ChunkSource,
    ValidationResult,
    normalize_text,
    validate_tour_against_chunks,
)

__all__ = [
    "CheckIssue",
    "CheckKind",
    "ChunkSource",
    "ValidationResult",
    "normalize_text",
    "validate_tour_against_chunks",
    "resolve_repo_file",
    "count_repo_file_lines",
    "parse_tour_payload",
    "validate_tour_artifact",
    "validate_tour",
]


def resolve_repo_file(repo_root: Path, file_path: str) -> Path | None:
    """Resolve a repo-relative path, rejecting absolute paths and traversal."""
    if file_path.startswith(("/", "\\")):
        return None
    if ".." in Path(file_path).parts:
        return None
    root = repo_root.resolve()
    full = (root / file_path).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return None
    return full


def _read_file_lines(path: Path) -> list[str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text.splitlines()


def count_repo_file_lines(path: Path) -> int | None:
    """Line count of a file, or ``None`` when it can't be read."""
    lines = _read_file_lines(path)
    return None if lines is None else len(lines)


def parse_tour_payload(payload: str | bytes | dict[str, Any]) -> tuple[TourArtifact | None, list[CheckIssue]]:
    """Parse JSON/dict into a tour artifact; return schema issues on failure."""
    if isinstance(payload, (str, bytes)):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, [
                CheckIssue(
                    kind=CheckKind.SCHEMA,
                    message=f"invalid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}",
                )
            ]
        except UnicodeDecodeError as exc:
            return None, [
                CheckIssue(
                    kind=CheckKind.SCHEMA,
                    message=f"invalid JSON: could not decode {exc.encoding} bytes: {exc.reason}",
                )
            ]
    else:
        data = payload

    try:
        return TourArtifact.model_validate(data), []
    except ValidationError as exc:
        issues = [
            CheckIssue(
                kind=CheckKind.SCHEMA,
                message=f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}",
            )
            for err in exc.errors()
        ]
        return None, issues


def validate_tour_artifact(artifact: TourArtifact, repo_root: Path) -> ValidationResult:
    """Check every step against files under ``repo_root``."""
    issues: list[CheckIssue] = []

    for step_index, step in enumerate(artifact.steps):
        resolved = resolve_repo_file(repo_root, step.file_path)
        if resolved is None:
            issues.append(
                CheckIssue(
                    kind=CheckKind.PATH_EXISTS,
                    step_index=step_index,
                    message=f"unsafe or invalid file_path: {step.file_path!r}",
                )
            )
            continue

        if not resolved.is_file():
            issues.append(
                CheckIssue(
                    kind=CheckKind.PATH_EXISTS,
                    step_index=step_index,
                    message=f"file not found: {step.file_path!r}",
                )
            )
            continue

        lines = _read_file_lines(resolved)
        if lines is None:
            issues.append(
                CheckIssue(
                    kind=CheckKind.PATH_EXISTS,
                    step_index=step_index,
                    message=f"file not readable: {step.file_path!r}",
                )
            )
            continue

        n_lines = len(lines)
        if step.start_line > n_lines or step.end_line > n_lines:
            issues.append(
                CheckIssue(
                    kind=CheckKind.LINES_IN_BOUNDS,
                    step_index=step_index,
                    message=(
                        f"lines {step.start_line}-{step.end_line} out of bounds "
                        f"for {step.file_path!r} ({n_lines} lines)"
                    ),
                )
            )
            continue

        slice_text = "\n".join(lines[step.start_line - 1 : step.end_line])
        normalized_slice = normalize_text(slice_text)
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
        if normalized_snippet not in normalized_slice:
            issues.append(
                CheckIssue(
                    kind=CheckKind.SNIPPET_MATCHES,
                    step_index=step_index,
                    message=(
                        f"snippet does not match source at "
                        f"{step.file_path!r}:{step.start_line}-{step.end_line}"
                    ),
                )
            )

    return ValidationResult(issues=issues)


def validate_tour(payload: str | bytes | dict[str, Any], repo_root: Path) -> ValidationResult:
    """Parse ``payload`` and run all structural checks against ``repo_root``.

    ``parse_tour_payload`` returns either ``(None, issues)`` or ``(artifact, [])``,
    so a non-None artifact always carries no schema issues — there's nothing to
    merge here.
    """
    artifact, schema_issues = parse_tour_payload(payload)
    if artifact is None:
        return ValidationResult(issues=schema_issues)

    return validate_tour_artifact(artifact, repo_root)
