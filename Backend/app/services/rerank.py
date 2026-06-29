"""Cross-encoder reranking for hybrid search (Exp 6).

After RRF fusion, score (query, chunk_text) pairs with a lightweight
cross-encoder and re-sort before the final top-k cut. Query-time only —
hybrid retrieval is unchanged.
"""

import logging
from dataclasses import replace
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

RERANK_MODEL = "BAAI/bge-reranker-base"
DEFAULT_RERANK_TOP_N = 30
_MAX_BODY_LINES = 12


def validate_rrf_weight(rrf_weight: float) -> None:
    """Reject blend weights outside the inclusive [0.0, 1.0] range."""
    if not 0.0 <= rrf_weight <= 1.0:
        raise ValueError("rrf_weight must be between 0.0 and 1.0")


@lru_cache(maxsize=2)
def _get_cross_encoder(model_name: str = RERANK_MODEL):
    from sentence_transformers import CrossEncoder

    logger.info("Loading cross-encoder model %s", model_name)
    return CrossEncoder(model_name)


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
    # result.signature is a normalized one-liner, so its newline count
    # undercounts a multi-line source signature and leaks parameter lines into
    # the body. Find where the signature actually ends in source (the def line
    # terminating in ":") instead of trusting the stored one-liner.
    sig_source_lines = 0
    if result.signature and body_lines:
        min_lines = result.signature.count("\n") + 1
        for i, line in enumerate(body_lines):
            if line.rstrip().endswith(":") and i + 1 >= min_lines:
                sig_source_lines = i + 1
                break
        else:
            sig_source_lines = min_lines
    body_start = body_lines[sig_source_lines:]
    if result.docstring:
        doc_lines = result.docstring.count("\n") + 1
        body_start = body_start[doc_lines:]
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
    rrf_weight: float = 0.9,
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
