"""Deterministic citation extraction — grounding by construction.

The model selects a chunk and a line span; it never authors the snippet or the
final line numbers. Here we derive ``file_path``/``start_line``/``end_line``/
``snippet`` directly from the chunk's stored source, so the resulting
``TourStep`` is guaranteed to quote real code at real lines (the structural
``PATH_EXISTS`` / ``LINES_IN_BOUNDS`` / ``SNIPPET_MATCHES`` checks cannot fail
for a step built this way).
"""

from __future__ import annotations

from app.models.tour import TourStep
from app.services.search import SearchResult


def _clamp_span(chunk: SearchResult, req_start: int, req_end: int) -> tuple[int, int]:
    """Map a requested absolute line span onto valid indices within ``chunk``.

    Returns 0-indexed ``(rel_start, rel_end)`` into ``chunk.source_code`` lines.
    Any request that is inverted, out of range, or otherwise unusable falls back
    to the whole chunk rather than raising — a slightly-too-wide snippet is a far
    better failure than a broken tour.
    """
    lines = chunk.source_code.splitlines()
    n = len(lines)
    if n == 0:
        return 0, 0

    rel_start = req_start - chunk.start_line
    rel_end = req_end - chunk.start_line

    valid = 0 <= rel_start <= rel_end < n
    if not valid:
        return 0, n - 1
    return rel_start, rel_end


def build_grounded_step(
    *,
    chunk: SearchResult,
    title: str,
    explanation: str,
    why: str | None,
    req_start: int,
    req_end: int,
) -> TourStep:
    """Assemble a ``TourStep`` whose citation is extracted from ``chunk``."""
    lines = chunk.source_code.splitlines()
    rel_start, rel_end = _clamp_span(chunk, req_start, req_end)
    snippet = "\n".join(lines[rel_start : rel_end + 1])

    return TourStep(
        title=title,
        explanation=explanation,
        file_path=chunk.file_path,
        start_line=chunk.start_line + rel_start,
        end_line=chunk.start_line + rel_end,
        snippet=snippet,
        why=(why or None),
    )
