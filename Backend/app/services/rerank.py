"""Cross-encoder reranking for hybrid search (Exp 6).

After RRF fusion, score (query, chunk_text) pairs with a lightweight
cross-encoder and re-sort before the final top-k cut. Query-time only —
hybrid retrieval is unchanged.
"""

import logging
import re
import threading
from dataclasses import replace
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

RERANK_MODEL = "BAAI/bge-reranker-base"
DEFAULT_RERANK_TOP_N = 30
DEFAULT_RERANK_RRF_WEIGHT = 0.9
_MAX_BODY_LINES = 12
_SIGNATURE_START = re.compile(r"^\s*(?:async\s+def|def|class)\s+")

# Serializes model creation so concurrent cache misses don't each spin up a
# CrossEncoder. lru_cache only guards its own dict, not the wrapped body.
_cross_encoder_lock = threading.Lock()


def validate_rrf_weight(rrf_weight: float) -> None:
    """Reject blend weights outside the inclusive [0.0, 1.0] range."""
    if not 0.0 <= rrf_weight <= 1.0:
        raise ValueError("rrf_weight must be between 0.0 and 1.0")


@lru_cache(maxsize=2)
def _load_cross_encoder(model_name: str):
    from sentence_transformers import CrossEncoder

    logger.info("Loading cross-encoder model %s", model_name)
    return CrossEncoder(model_name)


def _get_cross_encoder(model_name: str = RERANK_MODEL):
    # Single-flight: the first thread loads under the lock and populates the
    # lru_cache; threads that were waiting then hit the cache instead of
    # creating their own CrossEncoder. Cache hits acquire/release immediately.
    with _cross_encoder_lock:
        return _load_cross_encoder(model_name)


def _has_unescaped_delimiter(text: str, delimiter: str) -> bool:
    start = 0
    while True:
        idx = text.find(delimiter, start)
        if idx == -1:
            return False
        backslashes = 0
        pos = idx - 1
        while pos >= 0 and text[pos] == "\\":
            backslashes += 1
            pos -= 1
        if backslashes % 2 == 0:
            return True
        start = idx + len(delimiter)


def _skip_docstring_block(lines: list[str]) -> list[str]:
    """Drop a leading docstring literal (incl. ``\"\"\"``/``'''`` delimiters)."""
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return lines
    stripped = lines[idx].lstrip()
    for quote in ('"""', "'''"):
        if stripped.startswith(quote):
            rest = stripped[len(quote):]
            if _has_unescaped_delimiter(rest, quote):  # single-line docstring
                return lines[idx + 1:]
            for j in range(idx + 1, len(lines)):  # multi-line: find the close
                if _has_unescaped_delimiter(lines[j], quote):
                    return lines[j + 1:]
            return []  # unterminated; nothing usable remains
    return lines


def _source_signature_line_count(lines: list[str]) -> int:
    """Count the leading Python signature lines in a source chunk."""
    if not lines or not _SIGNATURE_START.match(lines[0]):
        return 0

    base_indent = len(lines[0]) - len(lines[0].lstrip())
    bracket_depth = 0
    for i, line in enumerate(lines):
        for char in line:
            if char in "([{":
                bracket_depth += 1
            elif char in ")]}":
                bracket_depth = max(0, bracket_depth - 1)
        stripped = line.strip()
        same_indent = len(line) - len(line.lstrip()) == base_indent
        if stripped.endswith(":") and same_indent and bracket_depth == 0:
            return i + 1

    return 1


def _build_rerank_text(result: Any) -> str:
    """Text paired with the query for cross-encoder scoring."""
    parts: list[str] = []
    if result.symbol_name:
        label = " ".join(p for p in (result.symbol_type, result.symbol_name) if p)
        parts.append(label)
    if result.file_path:
        parts.append(result.file_path)
    if result.signature:
        parts.append(result.signature)
    if result.docstring:
        parts.append(result.docstring)

    body_lines = (result.source_code or "").split("\n")
    sig_source_lines = 0
    if result.signature and body_lines:
        # result.signature is a normalized one-liner, so count the real source
        # signature and ignore nested/default-value lines that merely end in ":".
        sig_source_lines = _source_signature_line_count(body_lines)
    body_start = body_lines[sig_source_lines:]
    if result.docstring:
        # result.docstring is the stripped content (no quotes), so its line count
        # doesn't match how many *source* lines the docstring block spans once
        # the """/''' delimiters are included. Skip the literal block instead so
        # its text isn't fed to the cross-encoder twice (once via parts above).
        body_start = _skip_docstring_block(body_start)
    # Trim only blank edge lines — .strip() would drop the body's indentation.
    preview = "\n".join(body_start[:_MAX_BODY_LINES]).strip("\n")
    if preview.strip():
        parts.append(preview)

    return "\n".join(parts)


def rerank_results(
    query: str,
    results: list[Any],
    *,
    top_n: int = DEFAULT_RERANK_TOP_N,
    rrf_weight: float = DEFAULT_RERANK_RRF_WEIGHT,
    model_name: str = RERANK_MODEL,
) -> list[Any]:
    """Re-score hydrated results with a cross-encoder blended with RRF scores.

    Only the top ``top_n`` candidates are reranked; any results past that cut are
    appended below in their original RRF order so a caller slicing to a larger
    ``limit`` still receives enough rows. Returns copies of the reranked
    candidates with blended scores, leaving the input ``results`` untouched. Any
    model failure (download error, OOM, missing weights) is swallowed and the
    original RRF order is returned so the search endpoint degrades gracefully
    instead of crashing.
    """
    validate_rrf_weight(rrf_weight)
    # A negative top_n must mean "no rerank", but results[:top_n] would instead
    # slice off the last |top_n| rows, so reject it before any slicing.
    if top_n < 0:
        return results
    if len(results) <= 1:
        return results

    candidates = results[:top_n]
    if not candidates:
        return results

    try:
        model = _get_cross_encoder(model_name)
        pairs = [(query, _build_rerank_text(r)) for r in candidates]
        ce_scores = model.predict(pairs)
    except Exception:
        logger.exception("cross-encoder rerank failed; falling back to RRF order")
        return results

    rrf_scores = [r.score for r in candidates]
    rrf_min, rrf_max = min(rrf_scores), max(rrf_scores)
    rrf_span = rrf_max - rrf_min

    ce_list = [float(s) for s in ce_scores]
    ce_min, ce_max = min(ce_list), max(ce_list)
    ce_span = ce_max - ce_min

    ce_weight = 1.0 - rrf_weight
    reranked: list[Any] = []
    for r, ce_score in zip(candidates, ce_list):
        rrf_norm = (r.score - rrf_min) / rrf_span if rrf_span else 1.0
        ce_norm = (ce_score - ce_min) / ce_span if ce_span else 1.0
        blended = rrf_weight * rrf_norm + ce_weight * ce_norm
        reranked.append(replace(r, score=blended))

    reranked.sort(key=lambda r: r.score, reverse=True)
    return reranked + results[top_n:]
