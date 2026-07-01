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

This is the experiment harness for the retrieval-improvement loop: every knob
that affects retrieval is a CLI flag and is recorded in the JSON output, so a
run is fully reproducible from its config block.

Usage:
    uv run python -m eval.run_eval                       # k=5, retrieve 10, hybrid
    uv run python -m eval.run_eval --mode ablation       # vector vs fts vs hybrid
    uv run python -m eval.run_eval --fts-weight 1.5      # tune a knob
    uv run python -m eval.run_eval --label exp1 --out eval/runs/exp1.json
    uv run python -m eval.run_eval --json                # machine-readable output
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, create_engine

from app.config import settings
from app.services.rerank import DEFAULT_RERANK_RRF_WEIGHT, DEFAULT_RERANK_TOP_N
from app.services.search import RetrievalDebug, SearchResult, hybrid_search_debug

DATASET_PATH = Path(__file__).parent / "golden_dataset.json"

ABLATION_MODES = ("vector", "fts", "hybrid")


@dataclass
class RetrievalConfig:
    """Every knob that influences a retrieval run. Recorded in the output."""

    mode: str = "hybrid"
    k: int = 5
    limit: int = 10
    top_n: int = 60  # matches --top-n CLI default so RetrievalConfig() is comparable
    rrf_k: int = 60
    vector_weight: float = 1.0
    fts_weight: float = 1.0
    path_penalty: float = 1.0
    filter_demo_paths: bool = True
    rerank: bool = False
    rerank_top_n: int = DEFAULT_RERANK_TOP_N
    rerank_rrf_weight: float = DEFAULT_RERANK_RRF_WEIGHT
    rerank_model: str | None = None


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


def _resolve_relevant_ids(
    session: Session,
    repo_name: str,
    installation_id: int,
    questions: list[dict],
) -> dict[tuple[str, str], list[int]]:
    """Map each labeled (file, symbol) -> chunk_ids present in the index.

    Lets the eval report a relevant chunk's rank *within each retriever* even
    when it never reaches the final top-k, and flag labels that aren't indexed
    at all (parser/dataset mismatch rather than a retrieval miss).
    """
    wanted = {
        (r["file"], r["symbol"]) for q in questions for r in q["relevant"]
    }
    sql = text("""
        SELECT id, file_path, symbol_name
        FROM   code_chunks
        WHERE  repo_name = :repo_name
          AND  installation_id = :installation_id
    """)
    rows = session.execute(
        sql, {"repo_name": repo_name, "installation_id": installation_id}
    ).all()
    out: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        key = (r.file_path, r.symbol_name)
        if key in wanted:
            out[key].append(r.id)
    return out


def _diagnose(
    relevant: list[dict],
    relevant_ids: dict[tuple[str, str], list[int]],
    debug: RetrievalDebug,
    results: list[SearchResult],
    k: int,
) -> list[dict]:
    """Per relevant chunk: where each retriever ranked it, attributing misses."""
    final_rank = {
        (r.file_path, r.symbol_name): i for i, r in enumerate(results, 1)
    }
    out = []
    for rel in relevant:
        key = (rel["file"], rel["symbol"])
        ids = relevant_ids.get(key, [])
        vr = min(
            (debug.vector_ranks[c] for c in ids if c in debug.vector_ranks),
            default=None,
        )
        fr = min(
            (debug.fts_ranks[c] for c in ids if c in debug.fts_ranks),
            default=None,
        )
        fin = final_rank.get(key)
        out.append(
            {
                "file": rel["file"],
                "symbol": rel["symbol"],
                "indexed": bool(ids),
                "vector_rank": vr,
                "fts_rank": fr,
                "final_rank": fin,
                "found_at_k": fin is not None and fin <= k,
            }
        )
    return out


async def run(session: Session, cfg: RetrievalConfig, questions: list[dict],
              repo_name: str, installation_id: int,
              relevant_ids: dict[tuple[str, str], list[int]]) -> dict:
    rows = []
    for q in questions:
        results, debug = await hybrid_search_debug(
            session,
            q["question"],
            repo_name,
            installation_id=installation_id,
            top_n=cfg.top_n,
            rrf_k=cfg.rrf_k,
            limit=cfg.limit,
            vector_weight=cfg.vector_weight,
            fts_weight=cfg.fts_weight,
            mode=cfg.mode,
            path_penalty=cfg.path_penalty,
            filter_demo_paths=cfg.filter_demo_paths,
            rerank=cfg.rerank,
            rerank_top_n=cfg.rerank_top_n,
            rerank_rrf_weight=cfg.rerank_rrf_weight,
            rerank_model=cfg.rerank_model,
        )
        m = _metrics_for_question(results, q["relevant"], cfg.k)
        diagnosis = _diagnose(q["relevant"], relevant_ids, debug, results, cfg.k)
        top_preview = [
            f"{r.file_path.split('/')[-1]}:{r.symbol_name}"
            for r in results[: cfg.k]
        ]
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                **m,
                "diagnosis": diagnosis,
                "top_k_preview": top_preview,
            }
        )

    n = len(rows)
    agg = {
        "questions": n,
        f"hit_rate@{cfg.k}": sum(r["hit"] for r in rows) / n,
        f"recall@{cfg.k}": sum(r["recall"] for r in rows) / n,
        f"precision@{cfg.k}": sum(r["precision"] for r in rows) / n,
        "mrr": sum(r["rr"] for r in rows) / n,
    }
    return {"config": asdict(cfg), "aggregate": agg, "per_question": rows}


def _print_report(report: dict, label: str | None) -> None:
    cfg = report["config"]
    agg = report["aggregate"]
    k = cfg["k"]
    rows = report["per_question"]

    head = f"\nRetrieval eval | repo=tiangolo/fastapi | mode={cfg['mode']} | k={k} | limit={cfg['limit']}"
    if label:
        head += f" | label={label}"
    print(head)
    print(
        f"knobs: top_n={cfg['top_n']} rrf_k={cfg['rrf_k']} "
        f"vec_w={cfg['vector_weight']} fts_w={cfg['fts_weight']} "
        f"path_penalty={cfg['path_penalty']} filter_demo={cfg['filter_demo_paths']} "
        f"rerank={cfg.get('rerank', False)} "
        f"rerank_top_n={cfg.get('rerank_top_n', DEFAULT_RERANK_TOP_N)} "
        f"rerank_rrf_w={cfg.get('rerank_rrf_weight', DEFAULT_RERANK_RRF_WEIGHT)} "
        f"rerank_model={cfg.get('rerank_model') or 'default'}"
    )
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

    _print_diagnostics(rows, k)


def _fmt_rank(v: int | None) -> str:
    return str(v) if v is not None else "—"


def _print_diagnostics(rows: list[dict], k: int) -> None:
    """Attribute misses to a retriever and surface non-indexed labels."""
    not_indexed = [
        (r["id"], d["file"], d["symbol"])
        for r in rows
        for d in r["diagnosis"]
        if not d["indexed"]
    ]
    if not_indexed:
        print(f"NOT INDEXED ({len(not_indexed)}) — labeled chunk absent from index "
              "(parser/dataset issue, not retrieval):")
        for qid, f, s in not_indexed:
            print(f"  [{qid}] {f}:{s}")
        print()

    misses = [r for r in rows if not r["hit"]]
    if misses:
        print(f"MISSES ({len(misses)}) — nothing relevant in top-{k}. "
              "Per labeled chunk, rank within each retriever (— = outside top_n):")
        for r in misses:
            print(f"  [{r['id']}] {r['question']}")
            for d in r["diagnosis"]:
                print(
                    f"        {d['file']}:{d['symbol']:<28} "
                    f"vector={_fmt_rank(d['vector_rank']):>4}  "
                    f"fts={_fmt_rank(d['fts_rank']):>4}  "
                    f"final={_fmt_rank(d['final_rank']):>4}"
                )
        print()


def _print_ablation(reports: dict[str, dict], k: int) -> None:
    print(f"\nABLATION | repo=tiangolo/fastapi | k={k}")
    print("=" * 60)
    print(f"{'mode':<10}{'hit_rate':>10}{'recall':>10}{'mrr':>10}")
    print("-" * 60)
    for mode in ABLATION_MODES:
        agg = reports[mode]["aggregate"]
        print(
            f"{mode:<10}"
            f"{agg[f'hit_rate@{k}']:>10.3f}"
            f"{agg[f'recall@{k}']:>10.3f}"
            f"{agg['mrr']:>10.3f}"
        )
    print("=" * 60)

    print("\nPER-QUESTION HIT MATRIX (Y = >=1 relevant in top-k)")
    print(f"{'id':<5}" + "".join(f"{m:>8}" for m in ABLATION_MODES) + "  question")
    print("-" * 78)
    by_id = {
        mode: {r["id"]: r for r in reports[mode]["per_question"]}
        for mode in ABLATION_MODES
    }
    for r in reports["hybrid"]["per_question"]:
        qid = r["id"]
        cells = "".join(
            f"{'Y' if by_id[m][qid]['hit'] else '.':>8}" for m in ABLATION_MODES
        )
        q = r["question"]
        q = q if len(q) <= 40 else q[:37] + "..."
        print(f"{qid:<5}{cells}  {q}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=5, help="cutoff for @k metrics")
    parser.add_argument(
        "--limit", type=int, default=10, help="chunks retrieved per query"
    )
    parser.add_argument(
        "--top-n", type=int, default=60, help="results per retriever before fusion"
    )
    parser.add_argument("--rrf-k", type=int, default=60, help="RRF constant")
    parser.add_argument(
        "--vector-weight", type=float, default=1.0, help="RRF weight for vector list"
    )
    parser.add_argument(
        "--fts-weight", type=float, default=1.0, help="RRF weight for FTS list"
    )
    parser.add_argument(
        "--path-penalty",
        type=float,
        default=0.3,
        help="multiplier (<1.0) applied to test/tutorial chunks after fusion",
    )
    parser.add_argument(
        "--no-filter-demo-paths",
        action="store_true",
        help="include tests/tutorials/docs_src in retriever candidate pools",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="cross-encoder rerank top fused candidates before final cut (Exp 6)",
    )
    parser.add_argument(
        "--rerank-top-n",
        type=int,
        default=DEFAULT_RERANK_TOP_N,
        help="fused candidates passed to the cross-encoder when --rerank is set",
    )
    parser.add_argument(
        "--rerank-rrf-weight",
        type=float,
        default=DEFAULT_RERANK_RRF_WEIGHT,
        help="blend weight for RRF vs cross-encoder (1.0 = RRF only, 0.0 = CE only)",
    )
    parser.add_argument(
        "--rerank-model",
        default=None,
        help="cross-encoder model id (default: BAAI/bge-reranker-base)",
    )
    parser.add_argument(
        "--mode",
        choices=(*ABLATION_MODES, "ablation"),
        default="hybrid",
        help="retriever(s) to run; 'ablation' runs vector, fts and hybrid",
    )
    parser.add_argument(
        "--label", default=None, help="human label recorded in output / printed"
    )
    parser.add_argument(
        "--out", default=None, help="write the full report JSON to this path"
    )
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    args = parser.parse_args()

    k = min(args.k, args.limit)
    if k < args.k:
        print(
            f"warning: --k {args.k} exceeds --limit {args.limit}; "
            f"clamping k to {k} (metrics reported @{k})"
        )
    if not 0.0 <= args.rerank_rrf_weight <= 1.0:
        raise SystemExit(
            f"error: --rerank-rrf-weight {args.rerank_rrf_weight} is out of range "
            f"(must be between 0.0 and 1.0)"
        )
    data = json.loads(DATASET_PATH.read_text())
    repo_name = data["repo_name"]
    installation_id = data["installation_id"]
    questions = data["questions"]

    engine = create_engine(settings.database_url)

    async def _run_all() -> dict:
        with Session(engine) as session:
            relevant_ids = _resolve_relevant_ids(
                session, repo_name, installation_id, questions
            )
            modes = ABLATION_MODES if args.mode == "ablation" else (args.mode,)
            reports = {}
            for mode in modes:
                cfg = RetrievalConfig(
                    mode=mode,
                    k=k,
                    limit=args.limit,
                    top_n=args.top_n,
                    rrf_k=args.rrf_k,
                    vector_weight=args.vector_weight,
                    fts_weight=args.fts_weight,
                    path_penalty=args.path_penalty,
                    filter_demo_paths=not args.no_filter_demo_paths,
                    rerank=args.rerank,
                    rerank_top_n=args.rerank_top_n,
                    rerank_rrf_weight=args.rerank_rrf_weight,
                    rerank_model=args.rerank_model,
                )
                reports[mode] = await run(
                    session, cfg, questions, repo_name, installation_id, relevant_ids
                )
            return reports

    reports = asyncio.run(_run_all())

    if args.mode == "ablation":
        output = {"label": args.label, "ablation": reports}
        if args.json:
            print(json.dumps(output, indent=2))
        else:
            _print_ablation(reports, k)
    else:
        report = reports[args.mode]
        report["label"] = args.label
        output = report
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_report(report, args.label)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
