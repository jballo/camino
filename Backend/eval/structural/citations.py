"""Parse free-text code citations from agent answers and validate them structurally."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pathlib import Path

from eval.structural.validate import (
    CheckIssue,
    CheckKind,
    ValidationResult,
    count_repo_file_lines,
    resolve_repo_file,
)

# Repo-relative paths like ``fastapi/routing.py`` or ``app/services/search.py``.
# Directory segments are optional at the regex level so backticked root files can
# be parsed, but plain-text extraction applies stricter path-shape checks below.
_FILE_PATH = r"(?:[\w.-]+/)*[\w.-]+\.\w+"
# ``path:42``, ``path:42-50``, or ``path:symbol_name``.
_QUALIFIED = re.compile(
    rf"(?P<path>{_FILE_PATH})(?::(?P<suffix>[\w.-]+))?"
)
# Backtick-wrapped citations are preferred in agent answers.
_BACKTICK = re.compile(rf"`({_FILE_PATH}(?::[\w.-]+)?)`")
# URLs whose path component would otherwise be mis-parsed as a file citation
# (e.g. ``https://fastapi.tiangolo.com/tutorial/first-steps.md``).
_URL = re.compile(r"https?://\S+")
_COMMON_ROOT_FILES = frozenset(
    {
        ".env.example",
        "Cargo.toml",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE.md",
        "README.md",
        "README.rst",
        "SECURITY.md",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "pytest.ini",
        "requirements.txt",
        "ruff.toml",
        "setup.py",
        "tox.ini",
        "tsconfig.json",
        "uv.lock",
    }
)
_DOC_ROOT_FILE = re.compile(r"[A-Z][A-Z0-9_.-]*\.(?:md|rst|txt)")


@dataclass(frozen=True)
class CitationRef:
    """A code reference extracted from free text."""

    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    symbol: str | None = None
    raw: str = ""

    @property
    def key(self) -> tuple[str, int | None, int | None, str | None]:
        return (self.file_path, self.start_line, self.end_line, self.symbol)


def _parse_suffix(suffix: str | None) -> tuple[int | None, int | None, str | None]:
    if not suffix:
        return None, None, None

    if re.fullmatch(r"\d+", suffix):
        line = int(suffix)
        # Line numbers are 1-based; a 0 (or below) is not a real citation line.
        if line < 1:
            return None, None, None
        return line, line, None

    range_match = re.fullmatch(r"(\d+)-(\d+)", suffix)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        if start < 1 or end < 1:
            return None, None, None
        return start, end, None

    return None, None, suffix


def _parse_qualified(raw: str) -> CitationRef | None:
    match = _QUALIFIED.fullmatch(raw.strip())
    if not match:
        return None
    start_line, end_line, symbol = _parse_suffix(match.group("suffix"))
    return CitationRef(
        file_path=match.group("path"),
        start_line=start_line,
        end_line=end_line,
        symbol=symbol,
        raw=raw,
    )


def _looks_like_repo_file(path: str, *, allow_root_file: bool) -> bool:
    if "/" in path:
        return True
    if not allow_root_file:
        return False
    basename = path.rsplit("/", 1)[-1]
    return basename in _COMMON_ROOT_FILES or bool(_DOC_ROOT_FILE.fullmatch(basename))


def parse_citations(text: str) -> list[CitationRef]:
    """Extract de-duplicated code citations from agent answer text."""
    found: dict[tuple[str, int | None, int | None, str | None], CitationRef] = {}

    for match in _BACKTICK.finditer(text):
        ref = _parse_qualified(match.group(1))
        if ref is not None and _looks_like_repo_file(
            ref.file_path, allow_root_file=True
        ):
            found[ref.key] = ref

    # Bare (non-backticked) paths inside a URL are not real code citations.
    url_spans = [m.span() for m in _URL.finditer(text)]
    for match in _QUALIFIED.finditer(text):
        if any(match.start() < end and start < match.end() for start, end in url_spans):
            continue
        ref = _parse_qualified(match.group(0))
        if (
            ref is not None
            and _looks_like_repo_file(ref.file_path, allow_root_file=False)
            and ref.key not in found
        ):
            found[ref.key] = ref

    return list(found.values())


def validate_citations(citations: list[CitationRef], repo_root: Path) -> ValidationResult:
    """Check parsed citations against on-disk repo source (path + line bounds only)."""
    issues: list[CheckIssue] = []

    for index, citation in enumerate(citations):
        resolved = resolve_repo_file(repo_root, citation.file_path)
        if resolved is None:
            issues.append(
                CheckIssue(
                    kind=CheckKind.PATH_EXISTS,
                    step_index=index,
                    message=f"unsafe or invalid file_path: {citation.file_path!r}",
                )
            )
            continue

        if not resolved.is_file():
            issues.append(
                CheckIssue(
                    kind=CheckKind.PATH_EXISTS,
                    step_index=index,
                    message=f"file not found: {citation.file_path!r}",
                )
            )
            continue

        if citation.start_line is None and citation.end_line is None:
            continue

        line_count = count_repo_file_lines(resolved)
        if line_count is None:
            issues.append(
                CheckIssue(
                    kind=CheckKind.PATH_EXISTS,
                    step_index=index,
                    message=f"file not readable: {citation.file_path!r}",
                )
            )
            continue

        start = (
            citation.start_line
            if citation.start_line is not None
            else citation.end_line
        )
        end = (
            citation.end_line
            if citation.end_line is not None
            else citation.start_line
        )
        if start is None or end is None:
            continue

        if start < 1 or end < 1:
            issues.append(
                CheckIssue(
                    kind=CheckKind.LINES_IN_BOUNDS,
                    step_index=index,
                    message=(
                        f"line numbers must be 1-based, got {start}-{end} "
                        f"for {citation.file_path!r}"
                    ),
                )
            )
            continue

        if start > end:
            issues.append(
                CheckIssue(
                    kind=CheckKind.LINES_IN_BOUNDS,
                    step_index=index,
                    message=(
                        f"invalid line range {start}-{end} for {citation.file_path!r}"
                    ),
                )
            )
            continue

        if start > line_count or end > line_count:
            issues.append(
                CheckIssue(
                    kind=CheckKind.LINES_IN_BOUNDS,
                    step_index=index,
                    message=(
                        f"lines {start}-{end} out of bounds for "
                        f"{citation.file_path!r} ({line_count} lines)"
                    ),
                )
            )

    return ValidationResult(issues=issues)
