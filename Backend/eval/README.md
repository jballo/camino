# Eval Harnesses

Three eval harnesses live here:

1. **Retrieval eval** — does hybrid search surface the right chunks? (needs DB + OpenAI)
2. **Agent smoke eval** — does the live agent answer with structurally valid code citations? (needs DB + OpenAI)
3. **Structural eval** — does a tour artifact parse, reference real files, and quote matching source? (no LLM, no DB)

Retrieval work is paused at a strong baseline; structural eval is the active track for Phase 2 tour generation. Agent smoke eval is a lightweight end-to-end check over the current ReAct answer path.

## Files

- `golden_dataset.json` — 20 questions, each with hand-labeled relevant `(file, symbol)` chunks. Pinned to FastAPI `0.115.6`.
- `ingest_local.py` — ingests a local repo through the **real** production pipeline (same parser, embeddings, and `search_vector` SQL as `app/api/repositories.py`), reading from disk instead of GitHub.
- `run_eval.py` — runs each question through `hybrid_search` and reports hit rate, recall@k, precision@k, MRR.
- `run_agent_smoke_eval.py` — runs the live LangGraph agent on selected golden questions, parses answer citations, and validates citation paths/line ranges.
- `run_structural_eval.py` — runs tour JSON fixtures through schema + repo-grounding validators.
- `structural/` — tour schema validators (`validate.py`), citation validators (`citations.py`), hand-written pass/fail fixtures, and the smoke question manifest.
- `baseline_results.json` — recorded baseline numbers.
- `EXPERIMENTS.md` — **retrieval experiment log** (paused; resume from handoff section).
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
`--no-filter-demo-paths`, `--rerank`, `--rerank-top-n`, `--rerank-rrf-weight`,
`--rerank-model`, `--label`, `--out`, `--json`.

Diagnostics in the default report:

- **NOT INDEXED** — a labeled chunk that isn't in the index at all (parser/dataset
  bug, not a retrieval miss).
- **MISSES** — for each labeled chunk, its rank within the vector list, the FTS
  list, and the fused result (`—` = outside `top_n`). This attributes a miss to a
  specific retriever instead of guessing.
- **Ablation** — `--mode ablation` prints a hit_rate/recall/MRR comparison plus a
  per-question hit matrix so you can see where the two retrievers are complementary.

`runs/` is gitignored; use `--out eval/runs/<label>.json` to keep a leaderboard.

## Agent Smoke Eval

Runs the live ReAct agent against a small manifest of golden questions, then parses
free-text citations from the final answer and validates that the referenced files
and line ranges exist in the pinned FastAPI fixture repo.

This is intentionally lighter than retrieval eval: it checks end-to-end wiring,
source retrieval count, citation presence, and citation structure. It does not judge
whether the answer is semantically complete.

```bash
cd Backend
uv run python -m eval.ingest_local              # required once: DB + embeddings
uv run python -m eval.run_agent_smoke_eval
uv run python -m eval.run_agent_smoke_eval --question q02 --json
uv run python -m eval.run_agent_smoke_eval --strict
uv run pytest tests/test_agent_smoke_eval.py -q
```

The CLI auto-clones FastAPI `0.115.6` into `.data/fastapi` when missing, but it
still needs the fixture chunks indexed in Postgres. `OPENAI_API_KEY` is required
because the live agent uses the configured chat model.

## Structural eval (validator-only, no LLM)

Checks that a **structured tour artifact** is internally consistent and grounded in
the fixture repo:

| Check | What it catches |
|---|---|
| Schema | Malformed JSON, missing fields, invalid line ranges |
| Path exists | Hallucinated `file_path` values |
| Lines in bounds | `start_line` / `end_line` past EOF |
| Snippet matches | Quoted text not present at those lines |

Tour contract: `app/models/tour.py` (`TourArtifact`, `TourStep`).

```bash
cd Backend
uv run python -m eval.run_structural_eval
uv run python -m eval.run_structural_eval --fixture valid_minimal --json
uv run pytest tests/test_structural_eval.py -q
```

Fixtures live in `structural/fixtures/` (`valid_minimal`, `bad_path`, `bad_lines`,
`bad_snippet`, `bad_schema`). The CLI auto-clones FastAPI `0.115.6` into
`.data/fastapi` when missing (same pin as the retrieval golden set). No database or
OpenAI key required.

When the tour generation pipeline exists, point the same `validate_tour()` helper at
live agent output before persisting or returning tours.

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
