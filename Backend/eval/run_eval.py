"""Retrieval-quality eval for the FastAPI golden dataset.

Runs each golden question through the *real* production retrieval path
(``app.services.search.hybrid_search`` over Postgres with pgvector + FTS + RRF)
and reports standard IR metrics:

    - Hit rate @k   : fraction of questions with >=1 relevant chunk in the top-k
    - Recall @k     : (relevant chunks retrieved in top-k) / (total relevant labeled)
    - Precision @k  : (relevant chunks retrieved in top-k) / k
    - MRR           : mean reciprocal rank of the first relevant chunk

A retrieved chunk is "relevant" if its (file_path, symbol_name) matches one of
the hand-labeled pairs for that question.

Usage:
    uv run python -m eval.run_eval               # k=5, retrieve 10
    uv run python -m eval.run_eval --k 5 --limit 10
    uv run python -m eval.run_eval --json        # machine-readable output
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlmodel import Session, create_engine

from app.config import settings
from app.services.search import SearchResult, hybrid_search

DATASET_PATH = Path(__file__).parent / "golden_dataset.json"


def _relevant_ranks(
    results: list[SearchResult], relevant: list[dict]
) -> list[int]:
    """1-indexed ranks of retrieved chunks that match a labeled (file, symbol)."""
    wanted = {(r["file"], r["symbol"]) for r in relevant}
    ranks = []
    for i, res in enumerate(results, 1):
        if (res.file_path, res.symbol_name) in wanted:
            ranks.append(i)
    return ranks


def _metrics_for_question(
    results: list[SearchResult], relevant: list[dict], k: int
) -> dict:
    ranks = _relevant_ranks(results, relevant)
    ranks_at_k = [r for r in ranks if r <= k]
    n_relevant = len(relevant)

    hit = 1.0 if ranks_at_k else 0.0
    recall = len(ranks_at_k) / n_relevant if n_relevant else 0.0
    precision = len(ranks_at_k) / k if k else 0.0
    rr = 1.0 / ranks[0] if ranks else 0.0

    return {
        "hit": hit,
        "recall": recall,
        "precision": precision,
        "rr": rr,
        "first_rank": ranks[0] if ranks else None,
        "n_relevant": n_relevant,
        "n_found_at_k": len(ranks_at_k),
    }


async def run(k: int, limit: int) -> dict:
    data = json.loads(DATASET_PATH.read_text())
    repo_name = data["repo_name"]
    installation_id = data["installation_id"]
    questions = data["questions"]

    engine = create_engine(settings.database_url)

    rows = []
    with Session(engine) as session:
        for q in questions:
            results = await hybrid_search(
                session,
                q["question"],
                repo_name,
                installation_id=installation_id,
                limit=limit,
            )
            m = _metrics_for_question(results, q["relevant"], k)
            top_preview = [
                f"{r.file_path.split('/')[-1]}:{r.symbol_name}" for r in results[:k]
            ]
            rows.append(
                {
                    "id": q["id"],
                    "question": q["question"],
                    **m,
                    "top_k_preview": top_preview,
                }
            )

    n = len(rows)
    agg = {
        "questions": n,
        "k": k,
        "limit": limit,
        f"hit_rate@{k}": sum(r["hit"] for r in rows) / n,
        f"recall@{k}": sum(r["recall"] for r in rows) / n,
        f"precision@{k}": sum(r["precision"] for r in rows) / n,
        "mrr": sum(r["rr"] for r in rows) / n,
    }
    return {"aggregate": agg, "per_question": rows}


def _print_report(report: dict) -> None:
    agg = report["aggregate"]
    k = agg["k"]
    rows = report["per_question"]

    print(f"\nRetrieval eval | repo=tiangolo/fastapi | k={k} | limit={agg['limit']}")
    print("=" * 78)
    print(f"{'id':<5}{'hit':>4}{'rec':>6}{'prec':>6}{'rr':>6}  question")
    print("-" * 78)
    for r in rows:
        rr = f"{r['rr']:.2f}"
        rec = f"{r['recall']:.2f}"
        prec = f"{r['precision']:.2f}"
        hit = "Y" if r["hit"] else "."
        q = r["question"]
        q = q if len(q) <= 46 else q[:43] + "..."
        print(f"{r['id']:<5}{hit:>4}{rec:>6}{prec:>6}{rr:>6}  {q}")

    print("=" * 78)
    print("AGGREGATE")
    print(f"  questions     : {agg['questions']}")
    print(f"  hit_rate@{k}    : {agg[f'hit_rate@{k}']:.3f}")
    print(f"  recall@{k}      : {agg[f'recall@{k}']:.3f}")
    print(f"  precision@{k}   : {agg[f'precision@{k}']:.3f}")
    print(f"  MRR           : {agg['mrr']:.3f}")
    print()

    misses = [r for r in rows if not r["hit"]]
    if misses:
        print(f"MISSES ({len(misses)}) — nothing relevant in top-{k}:")
        for r in misses:
            print(f"  [{r['id']}] {r['question']}")
            print(f"        got: {r['top_k_preview']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=5, help="cutoff for @k metrics")
    parser.add_argument(
        "--limit", type=int, default=10, help="chunks retrieved per query"
    )
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    args = parser.parse_args()

    k = min(args.k, args.limit)
    report = asyncio.run(run(k, args.limit))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)


if __name__ == "__main__":
    main()
