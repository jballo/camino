import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from app.services.embeddings import embed_batch

logger = logging.getLogger(__name__)

DEFAULT_K = 60 # RRF constant
DEFAULT_TOP_N = 20 # results per retriever before fusion
DEFAULT_FINAL_LIMIT = 10

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
    top_n: int,
) -> list[tuple[int, int]]:
    """Returns list of (chunk_id, rank) ordered by cosine similarity."""
    sql = text("""
        SELECT e.chunk_id,
               ROW_NUMBER() OVER (ORDER BY e.embedding <=> CAST(:embedding AS vector)) AS rank
        FROM   code_chunk_embeddings e
        JOIN   code_chunks c ON c.id = e.chunk_id
        WHERE  c.repo_name = :repo_name
        ORDER  BY e.embedding <=> CAST(:embedding AS vector)
        LIMIT  :top_n
    """)
    rows = session.execute(
        sql,
        {"embedding": str(query_embedding), "repo_name": repo_name, "top_n": top_n},
    ).all()
    return [(r.chunk_id, r.rank) for r in rows]

def _fts_search(
    session: Session,
    query: str,
    repo_name: str,
    top_n: int,
) -> list[tuple[int, int]]:
    """Returns list of (chunk_id, rank) ordered by ts_rank."""
    sql = text("""
        SELECT c.id AS chunk_id,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank(c.search_vector,
                                    plainto_tsquery('english', :query)) DESC
               ) AS rank
        FROM   code_chunks c
        WHERE  c.repo_name = :repo_name
          AND  c.search_vector @@ plainto_tsquery('english', :query)
        ORDER  BY rank
        LIMIT  :top_n
    """)
    rows = session.execute(
        sql, {"query": query, "repo_name": repo_name, "top_n": top_n}
    ).all()
    return [(r.chunk_id, r.rank) for r in rows]


def _rrf_fuse(
    *ranked_lists: list[tuple[int, int]],
    k: int = DEFAULT_K,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion across N ranked lists.
    Each list is [(chunk_id, rank)].  Returns [(chunk_id, score)] sorted
    descending by fused score.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for chunk_id, rank in ranked:
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


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

async def hybrid_search(
    session: Session,
    query: str,
    repo_name: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    rrf_k: int = DEFAULT_K,
    limit: int = DEFAULT_FINAL_LIMIT,
) -> list[SearchResult]:
    """Run hybrid vector + FTS search with RRF fusion.
    This is the main entry point. It embeds the query, runs both retrievers
    in parallel (conceptually), fuses with RRF, and returns hydrated chunks.
    Designed to be called from both API endpoints and LangGraph nodes.
    """
    query_embedding = (await embed_batch([query]))[0]

    vector_ranked = _vector_search(session, query_embedding, repo_name, top_n)
    fts_ranked = _fts_search(session, query, repo_name, top_n)
    fused = _rrf_fuse(vector_ranked, fts_ranked, k=rrf_k)

    return _load_chunks(session, fused, limit)








