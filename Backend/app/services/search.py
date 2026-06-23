import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from app.services.embeddings import EMBED_MODEL, embed_batch

logger = logging.getLogger(__name__)

DEFAULT_K = 60 # RRF constant
DEFAULT_TOP_N = 60 # results per retriever before fusion (Exp 3: 20->60 lets deep
                   # vector hits reach fusion, e.g. APIRoute at vector rank ~59)
DEFAULT_FINAL_LIMIT = 10

# Substrings that mark a chunk as test/tutorial/example code. Such chunks are
# legitimately retrievable but should not crowd out library internals, so they
# get a multiplicative RRF-score penalty (see eval/EXPERIMENTS.md, Exp 3). The
# heuristic is path-based and conservative (real source rarely contains these).
DEMOTE_PATH_SUBSTRINGS = ("tests/", "test_", "docs_src/", "tutorial", "examples/")
# Exp 3: 0.3 recovers the Exp 1 FTS regression and lifts MRR 0.649->0.782 on the
# FastAPI golden set. 1.0 disables demotion; <1.0 demotes matching paths.
DEFAULT_PATH_PENALTY = 0.3
# Exp 5: exclude test/tutorial paths from the retriever candidate pool so top_n
# slots go to library internals (post-fusion demotion is too late for deep hits).
DEFAULT_FILTER_DEMO_PATHS = True


def _demo_path_exclusion_sql(alias: str = "c") -> str:
    """SQL AND-clause excluding chunks whose path contains demo/test markers."""
    col = f"{alias}.file_path"
    checks = " OR ".join(
        f"POSITION('{sub}' IN {col}) > 0" for sub in DEMOTE_PATH_SUBSTRINGS
    )
    return f"AND NOT ({checks})"

@dataclass
class SearchResult:
    chunk_id: int
    repo_name: str
    file_path: str
    symbol_name: str
    symbol_type: str
    language: str
    start_line: int
    end_line: int
    source_code: str
    signature: str
    docstring: str | None
    score: float          # fused RRF score


def _vector_search(
    session: Session,
    query_embedding: list[float],
    repo_name: str,
    installation_id: int,
    top_n: int,
    model_name: str = EMBED_MODEL,
    *,
    filter_demo_paths: bool = DEFAULT_FILTER_DEMO_PATHS,
) -> list[tuple[int, int]]:
    """Returns list of (chunk_id, rank) ordered by cosine similarity."""
    path_filter = _demo_path_exclusion_sql("c") if filter_demo_paths else ""
    sql = text(f"""
        SELECT e.chunk_id,
               ROW_NUMBER() OVER (
                   ORDER BY e.embedding <=> CAST(:embedding AS vector), e.chunk_id
               ) AS rank
        FROM   code_chunk_embeddings e
        JOIN   code_chunks c ON c.id = e.chunk_id
        WHERE  c.repo_name = :repo_name
          AND  c.installation_id = :installation_id
          AND  e.model_name = :model_name
          {path_filter}
        ORDER  BY e.embedding <=> CAST(:embedding AS vector), e.chunk_id
        LIMIT  :top_n
    """)
    rows = session.execute(
        sql,
        {
            "embedding": str(query_embedding),
            "repo_name": repo_name,
            "installation_id": installation_id,
            "model_name": model_name,
            "top_n": top_n,
        },
    ).all()
    return [(r.chunk_id, r.rank) for r in rows]

def _fts_search(
    session: Session,
    query: str,
    repo_name: str,
    installation_id: int,
    top_n: int,
    *,
    filter_demo_paths: bool = DEFAULT_FILTER_DEMO_PATHS,
) -> list[tuple[int, int]]:
    """Returns list of (chunk_id, rank) ordered by ts_rank.

    The query is built from ``plainto_tsquery`` (which safely lexizes/stems and
    drops stopwords) but its AND operators are rewritten to OR, so a chunk that
    matches *any* query term is a candidate and ``ts_rank`` orders by how many /
    how important the matches are. AND-semantics previously required every term
    to be present, which almost never holds for sparse code chunks — see
    eval/EXPERIMENTS.md (Exp 1). ``c.id`` is a deterministic rank tiebreaker.
    """
    path_filter = _demo_path_exclusion_sql("c") if filter_demo_paths else ""
    sql = text(f"""
        WITH q AS (
            SELECT replace(
                       plainto_tsquery('english', :query)::text, ' & ', ' | '
                   )::tsquery AS query
        )
        SELECT c.id AS chunk_id,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank(c.search_vector, q.query) DESC, c.id
               ) AS rank
        FROM   code_chunks c, q
        WHERE  c.repo_name = :repo_name
          AND  c.installation_id = :installation_id
          AND  c.search_vector @@ q.query
          {path_filter}
        ORDER  BY rank
        LIMIT  :top_n
    """)
    rows = session.execute(
        sql, {"query": query, "repo_name": repo_name, "installation_id": installation_id, "top_n": top_n}
    ).all()
    return [(r.chunk_id, r.rank) for r in rows]


def _rrf_fuse(
    *ranked_lists: list[tuple[int, int]],
    k: int = DEFAULT_K,
    weights: list[float] | None = None,
) -> list[tuple[int, float]]:
    """Weighted Reciprocal Rank Fusion across N ranked lists.
    Each list is [(chunk_id, rank)].  Returns [(chunk_id, score)] sorted
    descending by fused score. ``weights`` lets callers up/down-weight a
    retriever's contribution (e.g. boost FTS for exact-symbol queries);
    defaults to equal weight per list.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError(
            f"weights length {len(weights)} != ranked_lists length {len(ranked_lists)}"
        )
    scores: dict[int, float] = {}
    for ranked, weight in zip(ranked_lists, weights):
        for chunk_id, rank in ranked:
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _demote_paths(
    session: Session,
    fused: list[tuple[int, float]],
    penalty: float,
    substrings: tuple[str, ...] = DEMOTE_PATH_SUBSTRINGS,
) -> list[tuple[int, float]]:
    """Multiply the RRF score of test/tutorial chunks by ``penalty`` and re-sort.

    Applied to the full fused list *before* the top-k cut so demoted chunks fall
    below library internals. No-op when ``penalty == 1.0``.
    """
    if penalty == 1.0 or not fused:
        return fused
    ids = [cid for cid, _ in fused]
    rows = session.execute(
        text("SELECT id, file_path FROM code_chunks WHERE id = ANY(:ids)"),
        {"ids": ids},
    ).all()
    path_map = {r.id: r.file_path or "" for r in rows}
    rescored = [
        (
            cid,
            score * penalty
            if any(s in path_map.get(cid, "") for s in substrings)
            else score,
        )
        for cid, score in fused
    ]
    return sorted(rescored, key=lambda x: x[1], reverse=True)


def _load_chunks(
    session: Session,
    fused: list[tuple[int, float]],
    limit: int,
) -> list[SearchResult]:
    """Hydrate chunk_ids into full SearchResult objects, preserving rank order."""
    top = fused[:limit]
    if not top:
        return []
    ids = [cid for cid, _ in top]
    score_map = dict(top)
    sql = text("""
        SELECT id, repo_name, file_path, symbol_name, symbol_type,
               language, start_line, end_line, source_code, signature, docstring
        FROM   code_chunks
        WHERE  id = ANY(:ids)
    """)
    rows = session.execute(sql, {"ids": ids}).mappings().all()
    row_map = {r["id"]: r for r in rows}
    results = []
    for cid in ids:
        r = row_map.get(cid)
        if not r:
            continue
        results.append(SearchResult(
            chunk_id=r["id"],
            repo_name=r["repo_name"],
            file_path=r["file_path"],
            symbol_name=r["symbol_name"],
            symbol_type=r["symbol_type"],
            language=r["language"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            source_code=r["source_code"],
            signature=r["signature"],
            docstring=r["docstring"],
            score=score_map[cid],
        ))
    return results

@dataclass
class RetrievalDebug:
    """Per-retriever diagnostics for a single query.

    ``vector_ranks`` / ``fts_ranks`` map chunk_id -> 1-indexed rank within
    that retriever's top_n (chunks beyond top_n are absent). ``fused`` is the
    post-fusion [(chunk_id, score)] ordering. Used by the eval to attribute a
    miss to a specific retriever rather than guessing.
    """

    vector_ranks: dict[int, int]
    fts_ranks: dict[int, int]
    fused: list[tuple[int, float]]


async def hybrid_search_debug(
    session: Session,
    query: str,
    repo_name: str,
    *,
    installation_id: int,
    top_n: int = DEFAULT_TOP_N,
    rrf_k: int = DEFAULT_K,
    limit: int = DEFAULT_FINAL_LIMIT,
    vector_weight: float = 1.0,
    fts_weight: float = 1.0,
    mode: str = "hybrid",
    path_penalty: float = DEFAULT_PATH_PENALTY,
    filter_demo_paths: bool = DEFAULT_FILTER_DEMO_PATHS,
) -> tuple[list[SearchResult], RetrievalDebug]:
    """Retrieval core: hydrated results plus per-retriever diagnostics.

    ``mode`` selects which retrievers run ("hybrid" | "vector" | "fts"),
    enabling ablation studies. ``vector_weight``/``fts_weight`` tune each
    retriever's contribution to the RRF fusion. ``path_penalty`` (<1.0) demotes
    test/tutorial chunks after fusion. ``filter_demo_paths`` excludes those paths
    from the retriever candidate pools (Exp 5).
    """
    query_embedding = (await embed_batch([query]))[0]

    vector_ranked = (
        _vector_search(
            session,
            query_embedding,
            repo_name,
            installation_id,
            top_n,
            filter_demo_paths=filter_demo_paths,
        )
        if mode in ("hybrid", "vector")
        else []
    )
    fts_ranked = (
        _fts_search(
            session,
            query,
            repo_name,
            installation_id,
            top_n,
            filter_demo_paths=filter_demo_paths,
        )
        if mode in ("hybrid", "fts")
        else []
    )
    fused = _rrf_fuse(
        vector_ranked,
        fts_ranked,
        k=rrf_k,
        weights=[vector_weight, fts_weight],
    )
    fused = _demote_paths(session, fused, path_penalty)

    results = _load_chunks(session, fused, limit)
    debug = RetrievalDebug(
        vector_ranks={cid: rank for cid, rank in vector_ranked},
        fts_ranks={cid: rank for cid, rank in fts_ranked},
        fused=fused,
    )
    return results, debug


async def hybrid_search(
    session: Session,
    query: str,
    repo_name: str,
    *,
    installation_id: int,
    top_n: int = DEFAULT_TOP_N,
    rrf_k: int = DEFAULT_K,
    limit: int = DEFAULT_FINAL_LIMIT,
    vector_weight: float = 1.0,
    fts_weight: float = 1.0,
    mode: str = "hybrid",
    path_penalty: float = DEFAULT_PATH_PENALTY,
    filter_demo_paths: bool = DEFAULT_FILTER_DEMO_PATHS,
) -> list[SearchResult]:
    """Run hybrid vector + FTS search with RRF fusion.
    This is the main entry point. It embeds the query, runs both retrievers
    in parallel (conceptually), fuses with RRF, and returns hydrated chunks.
    Designed to be called from both API endpoints and LangGraph nodes.
    """
    results, _ = await hybrid_search_debug(
        session,
        query,
        repo_name,
        installation_id=installation_id,
        top_n=top_n,
        rrf_k=rrf_k,
        limit=limit,
        vector_weight=vector_weight,
        fts_weight=fts_weight,
        mode=mode,
        path_penalty=path_penalty,
        filter_demo_paths=filter_demo_paths,
    )
    return results








