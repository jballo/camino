"""Cross-encoder reranking for hybrid search (Exp 6).

After RRF fusion, score (query, chunk_text) pairs with a lightweight
cross-encoder and re-sort before the final top-k cut. Query-time only —
hybrid retrieval is unchanged.
"""

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

RERANK_MODEL = "BAAI/bge-reranker-base"
DEFAULT_RERANK_TOP_N = 30
_MAX_BODY_LINES = 12


@lru_cache(maxsize=2)
def _get_cross_encoder(model_name: str = RERANK_MODEL):
    from sentence_transformers import CrossEncoder

    logger.info("Loading cross-encoder model %s", model_name)
    return CrossEncoder(model_name)


def _build_rerank_text(result: Any) -> str:
    """Text paired with the query for cross-encoder scoring."""
    parts: list[str] = []
    if result.symbol_name:
        parts.append(f"{result.symbol_type} {result.symbol_name}")
    if result.file_path:
        parts.append(result.file_path)
    if result.signature:
        parts.append(result.signature)
    if result.docstring:
        parts.append(result.docstring)

    body_lines = result.source_code.split("\n")
    sig_lines = result.signature.count("\n") + 1 if result.signature else 0
    body_start = body_lines[sig_lines:]
    if result.docstring:
        doc_lines = result.docstring.count("\n") + 1
        body_start = body_start[doc_lines:]
    preview = "\n".join(body_start[:_MAX_BODY_LINES]).strip()
    if preview:
        parts.append(preview)

    return "\n".join(parts)


def rerank_results(
    query: str,
    results: list[Any],
    *,
    top_n: int = DEFAULT_RERANK_TOP_N,
    rrf_weight: float = 0.5,
    model_name: str = RERANK_MODEL,
) -> list[Any]:
    """Re-score hydrated results with a cross-encoder blended with RRF scores."""
    if len(results) <= 1:
        return results

    candidates = results[:top_n]
    model = _get_cross_encoder(model_name)
    pairs = [(query, _build_rerank_text(r)) for r in candidates]
    ce_scores = model.predict(pairs)

    rrf_scores = [r.score for r in candidates]
    rrf_min, rrf_max = min(rrf_scores), max(rrf_scores)
    rrf_span = rrf_max - rrf_min

    ce_list = [float(s) for s in ce_scores]
    ce_min, ce_max = min(ce_list), max(ce_list)
    ce_span = ce_max - ce_min

    ce_weight = 1.0 - rrf_weight
    for r, ce_score in zip(candidates, ce_list):
        rrf_norm = (r.score - rrf_min) / rrf_span if rrf_span else 1.0
        ce_norm = (ce_score - ce_min) / ce_span if ce_span else 1.0
        r.score = rrf_weight * rrf_norm + ce_weight * ce_norm

    candidates.sort(key=lambda r: r.score, reverse=True)
    return candidates
