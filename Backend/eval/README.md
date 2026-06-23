# Retrieval eval

Measures the quality of the **non-agent retrieval path** (`app.services.search.hybrid_search`:
pgvector + Postgres FTS fused with RRF) against a hand-labeled FastAPI golden set.

## Files

- `golden_dataset.json` — 20 questions, each with hand-labeled relevant `(file, symbol)` chunks. Pinned to FastAPI `0.115.6`.
- `ingest_local.py` — ingests a local repo through the **real** production pipeline (same parser, embeddings, and `search_vector` SQL as `app/api/repositories.py`), reading from disk instead of GitHub.
- `run_eval.py` — runs each question through `hybrid_search` and reports hit rate, recall@k, precision@k, MRR.
- `baseline_results.json` — recorded baseline numbers.
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
