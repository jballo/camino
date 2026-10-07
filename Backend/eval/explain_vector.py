"""Explain and time the production-shaped vector retriever query.

The query text comes from ``app.services.search`` so this diagnostic cannot
silently drift from production retrieval. Query embedding time is intentionally
excluded from the reported SQL latency.

Examples:
    uv run python -m eval.explain_vector
    uv run python -m eval.explain_vector --dims 768
    uv run python -m eval.explain_vector --question-id q03 --iterations 20
    uv run python -m eval.explain_vector \
        --dataset eval/golden_dataset_deepeval.json --question-id q01
    uv run python -m eval.explain_vector \
        --repo confident-ai/deepeval --ref main --query "How are evals run?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, create_engine

from app.config import settings
from app.services.embeddings import EMBED_DIMENSIONS, EMBED_MODEL, embed_batch
from app.services.search import DEFAULT_TOP_N, _vector_search_sql
from eval.run_eval import DATASET_PATH


def _percentile(values: list[float], quantile: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _question(dataset: dict, question_id: str) -> str:
    for item in dataset["questions"]:
        if item["id"] == question_id:
            return item["question"]
    available = ", ".join(item["id"] for item in dataset["questions"])
    raise SystemExit(
        f"unknown --question-id {question_id!r}; available ids: {available}"
    )


async def _run(args: argparse.Namespace) -> None:
    dataset = json.loads(args.dataset.read_text())
    repo_name = args.repo or dataset["repo_name"]
    ref = args.ref or dataset.get("ref") or dataset["repo_version"]
    query = args.query or _question(dataset, args.question_id)
    query_label = "custom" if args.query else args.question_id

    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        generation = session.execute(
            text("""
                SELECT active_generation
                FROM repo_index_state
                WHERE repo_name = :repo_name
                  AND ref = :ref
            """),
            {"repo_name": repo_name, "ref": ref},
        ).scalar_one_or_none()
    if generation is None:
        raise SystemExit(
            f"no active index for repo={repo_name!r} ref={ref!r}; "
            "run eval.ingest_local first"
        )

    query_embedding = (await embed_batch([query]))[0]
    with Session(engine) as session:
        if args.hnsw_ef_search is not None:
            session.execute(
                text("SELECT set_config('hnsw.ef_search', :value, true)"),
                {"value": str(args.hnsw_ef_search)},
            )
        if args.hnsw_relaxed_order:
            session.execute(
                text(
                    "SELECT set_config("
                    "'hnsw.iterative_scan', 'relaxed_order', true)"
                )
            )
        query_sql = _vector_search_sql(
            filter_demo_paths=True,
            vector_dims=args.dims,
        )
        params: dict[str, object] = {
            "embedding": str(query_embedding),
            "repo_name": repo_name,
            "ref": ref,
            "generation": generation,
            "model_name": EMBED_MODEL,
            "top_n": args.top_n,
        }
        if args.dims is not None:
            params["vector_dims"] = args.dims

        plan_rows = session.execute(
            text(f"EXPLAIN (ANALYZE, BUFFERS) {query_sql}"),
            params,
        ).all()
        plan = "\n".join(str(row[0]) for row in plan_rows)

        timings_ms: list[float] = []
        row_counts: list[int] = []
        statement = text(query_sql)
        for _ in range(args.iterations):
            started = time.perf_counter()
            rows = session.execute(statement, params).all()
            timings_ms.append((time.perf_counter() - started) * 1000)
            row_counts.append(len(rows))

    print(
        "Vector retriever diagnostic "
        f"| repo={repo_name} | ref={ref} | question={query_label} "
        f"| vector_type={settings.vector_type} "
        f"| vector_index={settings.vector_index} "
        f"| dims={args.dims or EMBED_DIMENSIONS} | top_n={args.top_n} "
        f"| hnsw_ef_search={args.hnsw_ef_search or 'default'} "
        f"| hnsw_iterative_scan="
        f"{'relaxed_order' if args.hnsw_relaxed_order else 'default'}"
    )
    print("=" * 78)
    print(plan)
    print("=" * 78)
    print(
        f"ix_embeddings_hnsw used: "
        f"{'yes' if 'ix_embeddings_hnsw' in plan else 'no'}"
    )
    print(f"rows returned: min={min(row_counts)} max={max(row_counts)}")
    print(
        f"SQL latency ({args.iterations} runs): "
        f"p50={statistics.median(timings_ms):.2f} ms "
        f"p95={_percentile(timings_ms, 0.95):.2f} ms"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DATASET_PATH,
        help=f"golden dataset used for repo/ref and question text (default: {DATASET_PATH})",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="override the dataset repository (requires --ref and --query)",
    )
    parser.add_argument(
        "--ref",
        default=None,
        help="override the dataset ref (requires --repo)",
    )
    parser.add_argument(
        "--question-id",
        default="q01",
        help="golden-dataset question to embed (default: q01)",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="custom query text (overrides --question-id)",
    )
    parser.add_argument(
        "--dims",
        type=int,
        default=None,
        help="compare only the first N dimensions (exact scan)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help="maximum vector rows returned (default: production top_n)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=20,
        help="number of timed SQL executions",
    )
    parser.add_argument(
        "--hnsw-ef-search",
        type=int,
        default=None,
        help="set hnsw.ef_search for this diagnostic transaction",
    )
    parser.add_argument(
        "--hnsw-relaxed-order",
        action="store_true",
        help="set hnsw.iterative_scan=relaxed_order (pgvector 0.8+)",
    )
    args = parser.parse_args()
    if (args.repo is None) != (args.ref is None):
        parser.error("--repo and --ref must be provided together")
    if args.repo is not None and args.query is None:
        parser.error("--repo/--ref overrides require --query")
    if args.dims is not None and not 1 <= args.dims <= EMBED_DIMENSIONS:
        parser.error(f"--dims must be between 1 and {EMBED_DIMENSIONS}")
    if args.top_n < 1:
        parser.error("--top-n must be positive")
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.hnsw_ef_search is not None and args.hnsw_ef_search < 1:
        parser.error("--hnsw-ef-search must be positive")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
