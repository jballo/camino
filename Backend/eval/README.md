# Retrieval eval

Measures the quality of the **non-agent retrieval path** (`app.services.search.hybrid_search`:
pgvector + Postgres FTS fused with RRF) against a hand-labeled FastAPI golden set.

## Files

- `golden_dataset.json` — 20 questions, each with hand-labeled relevant `(file, symbol)` chunks. Pinned to FastAPI `0.115.6`.
- `ingest_local.py` — ingests a local repo through the **real** production pipeline (same parser, embeddings, and `search_vector` SQL as `app/api/repositories.py`), reading from disk instead of GitHub.
- `run_eval.py` — runs each question through `hybrid_search` and reports hit rate, recall@k, precision@k, MRR.
- `baseline_results.json` — recorded baseline numbers.
- `EXPERIMENTS.md` — **experiment log**: results, stacking notes, shipped config, next steps (start here when continuing in a new chat).
- `.data/` — the cloned fixture source. **Gitignored** (not committed); fetched on demand, see below.

## Reproduce from a fresh checkout

The FastAPI source is not committed. `ingest_local.py` auto-clones the pinned
version into `.data/` (gitignored) on first run, so reproduction is two commands:

```bash
cd Backend
uv run python -m eval.ingest_local      # clones FastAPI 0.115.6 if missing, then ingests
uv run python -m eval.run_eval --k 5 --limit 10
```

Both steps need network: ingest calls OpenAI to embed chunks, eval embeds each query.

Use `--no-clone` to ingest an already-present path only. To re-fetch a clean
fixture, delete `eval/.data/` and re-run.

## Experiment loop

`run_eval.py` is the harness for the retrieval-improvement loop. Every knob that
affects retrieval is a flag and is recorded in the output's `config` block, so a
run is reproducible from its config alone.

```bash
uv run python -m eval.run_eval --mode ablation        # vector vs fts vs hybrid
uv run python -m eval.run_eval --fts-weight 1.5        # tune RRF weights
uv run python -m eval.run_eval --top-n 40 --rrf-k 60   # tune fusion inputs
uv run python -m eval.run_eval --label exp1 --out eval/runs/exp1.json
```

Flags: `--mode {hybrid,vector,fts,ablation}`, `--k`, `--limit`, `--top-n`,
`--rrf-k`, `--vector-weight`, `--fts-weight`, `--path-penalty` (default 0.3),
`--no-filter-demo-paths`, `--label`, `--out`, `--json`.

Diagnostics in the default report:

- **NOT INDEXED** — a labeled chunk that isn't in the index at all (parser/dataset
  bug, not a retrieval miss).
- **MISSES** — for each labeled chunk, its rank within the vector list, the FTS
  list, and the fused result (`—` = outside `top_n`). This attributes a miss to a
  specific retriever instead of guessing.
- **Ablation** — `--mode ablation` prints a hit_rate/recall/MRR comparison plus a
  per-question hit matrix so you can see where the two retrievers are complementary.

`runs/` is gitignored; use `--out eval/runs/<label>.json` to keep a leaderboard.

## Baseline (FastAPI 0.115.6, k=5, retrieve 10)

| Metric | Value |
|---|---|
| Hit rate@5 | 0.800 |
| Recall@5 | 0.767 |
| Precision@5 | 0.170 |
| MRR | 0.649 |

The whole repo is ingested (including `tests/` and `docs_src/`) to match production
ingest behavior; those paths are the main source of misses, crowding out library
internals for several queries.
