# Eval Harnesses

Six eval harnesses live here:

1. **Retrieval eval** — does hybrid search surface the right chunks? (needs DB + OpenAI)
2. **Agent smoke eval** — does the live agent answer with structurally valid code citations? (needs DB + OpenAI)
3. **Structural eval** — does a tour artifact parse, reference real files, and quote matching source? (no LLM, no DB)
4. **Live tour smoke eval** — can the generation graph produce a grounded tour end to end? (needs DB + OpenAI)
5. **Tour judge eval** — does an LLM judge score generated tours for faithfulness, relevance, completeness, and ordering? (needs OpenAI; DB only for live generation)
6. **Vector SQL diagnostic** — does the production vector query use HNSW, and what are its p50/p95 SQL latencies? (needs DB + OpenAI)

Exp7 selected halfvec exact scan (V2), and the application defaults now match that
variant; the matrix below remains the reproduction procedure. Tour evaluation has
both deterministic grounding checks and an
LLM-as-judge baseline for quality trends.
Agent smoke eval remains a lightweight end-to-end check over the current ReAct answer
path.

## Files

- `golden_dataset.json` — 20 questions, each with hand-labeled relevant `(file, symbol)` chunks. Pinned to FastAPI `0.115.6`.
- `ingest_local.py` — ingests a local fixture with the production parser, embedding
  text, `search_vector` SQL, generation tags, and active-generation registry. It reads
  from disk and bypasses the GitHub snapshot and shared job queue.
- `run_eval.py` — runs each question through `hybrid_search` and reports hit rate, recall@k, precision@k, MRR.
- `explain_vector.py` — embeds one golden query, prints `EXPLAIN (ANALYZE, BUFFERS)` for the production vector SQL, and measures p50/p95 SQL latency. Use `--dataset PATH`, or `--repo NAME --ref REF --query TEXT`, to target a non-FastAPI index.
- `run_agent_smoke_eval.py` — runs the live LangGraph agent on selected golden questions, parses answer citations, and validates citation paths/line ranges.
- `run_structural_eval.py` — runs tour JSON fixtures through schema + repo-grounding validators.
- `run_tour_smoke_eval.py` — generates live tours for smoke topics and validates their grounded artifact shape.
- `run_tour_judge_eval.py` — scores live or fixture tours with the judge rubric.
- `judge/` — structured judge schemas, prompt, model call, and aggregate score reduction.
- `structural/` — tour schema validators (`validate.py`), citation validators (`citations.py`), hand-written pass/fail fixtures, and the smoke question manifest.
- `baseline_results.json` — recorded retrieval baseline numbers.
- `judge_baseline.json` — recorded tour judge baseline (aggregate + per-topic scores).
- `EXPERIMENTS.md` — **retrieval experiment log** (paused; resume from handoff section).
- `.data/` — the cloned fixture source. **Gitignored** (not committed); fetched on demand, see below.

## Reproduce from a fresh checkout

The FastAPI source is not committed. `ingest_local.py` auto-clones the pinned
version into `.data/` (gitignored) on first run, so reproduction is two commands:

Start the API once against a fresh database before running the harnesses so startup
creates `repo_index_state`, `live_code_chunks`, and the required indexes. The API does
not need to remain running during eval commands.

```bash
cd Backend
uv run python -m eval.ingest_local      # clones FastAPI 0.115.6 if missing, then ingests
uv run python -m eval.run_eval --k 5 --limit 10
```

Both steps need network: ingest calls OpenAI to embed chunks, eval embeds each query.
Eval preflight checks and retrieval read through `live_code_chunks`, so only the
generation published by `ingest_local.py` is visible to the harnesses.
Unlike production ingestion, which bounds memory with `INGEST_WAVE_CHUNKS` and commits
staged waves, the fixture ingester builds the fixture in memory and replaces/publishes
it in one transaction. Embedding rows are flushed in small batches within that
transaction to avoid oversized SQL statements. It exercises generation-scoped search,
but not worker leases, wave retries, or cancellation.

Use `--no-clone` to ingest an already-present path only. To re-fetch a clean
fixture, delete `eval/.data/` and re-run.

## Experiment loop

`run_eval.py` is the harness for the retrieval-improvement loop. Every knob that
affects retrieval is a flag and is recorded in the output's `config` block, so a
run is reproducible from its config alone.

The optional exp6 cross-encoder reranker is not installed by the default dependency
set. Install it before using `--rerank`:

```bash
uv sync --extra rerank
```

```bash
uv run python -m eval.run_eval --mode ablation        # vector vs fts vs hybrid
uv run python -m eval.run_eval --fts-weight 1.5        # tune RRF weights
uv run python -m eval.run_eval --top-n 40 --rrf-k 60   # tune fusion inputs
uv run python -m eval.run_eval --label exp1 --out eval/runs/exp1.json
```

Flags: `--dataset PATH`, `--mode {hybrid,vector,fts,ablation}`, `--k`, `--limit`, `--top-n`,
`--rrf-k`, `--vector-weight`, `--fts-weight`, `--path-penalty` (default 0.3),
`--no-filter-demo-paths`, `--rerank`, `--rerank-top-n`, `--rerank-rrf-weight`,
`--rerank-model`, `--vector-dims`, `--label`, `--out`, `--json`.

## Exp7 halfvec experiment matrix

Run every variant against the local throwaway test database. Start the API once
against a fresh database before ingesting so startup creates the schema. The
commands below pin `DATABASE_URL` to the documented local-only credentials so an
experiment cannot accidentally run against Neon.

```bash
# From anywhere, first use: create and start the dedicated testdb container.
docker run --name camino-exp7-testdb \
  -e POSTGRES_USER=loadtest \
  -e POSTGRES_PASSWORD=loadtest \
  -e POSTGRES_DB=loadtest \
  -p 5433:5432 \
  -v camino-exp7-testdb-data:/var/lib/postgresql/data \
  -d pgvector/pgvector:pg16

# On later uses, when that container already exists:
docker start camino-exp7-testdb

# Proceed once this reports "accepting connections".
docker exec camino-exp7-testdb pg_isready -U loadtest -d loadtest

# From Backend/: keep every command in this shell pinned to the local testdb.
export EXP7_DATABASE_URL=postgresql://loadtest:loadtest@localhost:5433/loadtest

# Run commands through Doppler while overriding its database with the local testdb.
exp7() {
  doppler run -- env DATABASE_URL="$EXP7_DATABASE_URL" "$@"
}

# Boot once, wait for startup, then stop it with Ctrl-C.
exp7 VECTOR_TYPE=vector VECTOR_INDEX=hnsw \
  uv run fastapi run app/main.py --port 8002

# Confirm the server has pgvector >= 0.7 before continuing.
psql "$EXP7_DATABASE_URL" \
  -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

```bash
# V0: fp32 + HNSW baseline
exp7 VECTOR_TYPE=vector VECTOR_INDEX=hnsw uv run python -m eval.ingest_local
exp7 VECTOR_TYPE=vector VECTOR_INDEX=hnsw uv run python -m eval.run_eval \
  --label exp7_v0_fp32_hnsw --out eval/runs/exp7_v0.json
exp7 VECTOR_TYPE=vector VECTOR_INDEX=hnsw uv run python -m eval.explain_vector

# Optional HNSW candidate-starvation probes (iterative scan needs pgvector 0.8+).
exp7 VECTOR_TYPE=vector VECTOR_INDEX=hnsw uv run python -m eval.explain_vector \
  --hnsw-ef-search 100
exp7 VECTOR_TYPE=vector VECTOR_INDEX=hnsw uv run python -m eval.explain_vector \
  --hnsw-ef-search 100 --hnsw-relaxed-order
```

Apply the V1 testdb migration with `psql`:

```bash
psql "$EXP7_DATABASE_URL" <<'SQL'
DROP INDEX IF EXISTS ix_embeddings_hnsw;
ALTER TABLE code_chunk_embeddings
  ALTER COLUMN embedding TYPE halfvec(1536) USING embedding::halfvec(1536);
ALTER TABLE code_chunk_embeddings ALTER COLUMN embedding SET STORAGE PLAIN;
VACUUM FULL code_chunk_embeddings;
CREATE INDEX ix_embeddings_hnsw ON code_chunk_embeddings
  USING hnsw (embedding halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64);
SQL
```

```bash
# V1: halfvec + HNSW
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=hnsw uv run python -m eval.run_eval \
  --label exp7_v1_halfvec_hnsw --out eval/runs/exp7_v1.json
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=hnsw uv run python -m eval.explain_vector
```

Drop the index for V2, then run:

```bash
psql "$EXP7_DATABASE_URL" -c "DROP INDEX IF EXISTS ix_embeddings_hnsw;"

# V2: halfvec exact scan
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none uv run python -m eval.run_eval \
  --label exp7_v2_halfvec_noindex --out eval/runs/exp7_v2.json
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none uv run python -m eval.explain_vector

# V3: query-time truncation (exact scan)
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none uv run python -m eval.run_eval \
  --vector-dims 768 --label exp7_v3_768 --out eval/runs/exp7_v3_768.json
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none uv run python -m eval.run_eval \
  --vector-dims 512 --label exp7_v3_512 --out eval/runs/exp7_v3_512.json
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none uv run python -m eval.explain_vector --dims 768
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none uv run python -m eval.explain_vector --dims 512
```

Record the table size after each schema variant and the index size for V0/V1:

```bash
psql "$EXP7_DATABASE_URL" \
  -c "SELECT pg_size_pretty(pg_total_relation_size('code_chunk_embeddings'));"
psql "$EXP7_DATABASE_URL" \
  -c "SELECT pg_size_pretty(pg_relation_size('ix_embeddings_hnsw'));"
```

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

The live generation pipeline uses the same grounding contract before persisted tours
are returned to the frontend.

## Live tour smoke eval

Runs the real Plan → Retrieve → Draft → Review pipeline against the indexed FastAPI
fixture, then applies the structural grounding validator to every generated artifact.
It requires Postgres with the fixture ingested and `OPENAI_API_KEY`.

```bash
cd Backend
uv run python -m eval.ingest_local
uv run python -m eval.run_tour_smoke_eval
uv run python -m eval.run_tour_smoke_eval --topic "request lifecycle" --json
uv run python -m eval.run_tour_smoke_eval --strict
```

Use `--model` to override the generator model, `--search-limit` to change candidates per
step, and `--out` to save the full report.

## Tour judge eval (LLM-as-judge)

Structural eval proves a tour is *grounded* (real files, matching snippets). It cannot
tell you whether the prose is any good. The judge eval scores the qualities structure
can't see, on a 1-5 scale:

| Dimension | Grain | What it asks |
|---|---|---|
| Faithfulness | per step | Is the explanation supported by the cited snippet (no invented behaviour)? |
| Relevance | per step | Does this step actually serve the tour topic? |
| Completeness | per tour | Do the steps together cover the topic's important aspects? |
| Ordering | per tour | Do the steps flow in a logical teaching order? |

One structured-output judge call scores a whole tour (the model sees every step in
order, so completeness/ordering get full context). Per-step faithfulness/relevance are
averaged; `overall` is the mean of the four dimensions. `--min-score` (default 3.5) is
the pass bar; `--strict` exits non-zero if any judged tour falls below it.

```bash
cd Backend
# live: generate a fresh tour per topic, then judge (needs DB + OpenAI)
uv run python -m eval.ingest_local
uv run python -m eval.run_tour_judge_eval
uv run python -m eval.run_tour_judge_eval --topic "dependency injection" --json

# fixture: judge a saved artifact only (OpenAI, no DB / no generation)
uv run python -m eval.run_tour_judge_eval --from-fixture valid_minimal

# CI gate + a stronger, separate judge model to reduce self-preference bias
uv run python -m eval.run_tour_judge_eval --strict --min-score 3.5 --judge-model gpt-4o
uv run pytest tests/test_tour_judge.py -q
```

The judge is a calibrated but imperfect signal — see "Known limitations & failure
modes" in `docs/tour-generation.md`. Treat scores as a directional regression signal,
not ground truth; grounding is still enforced deterministically by the structural eval.

**Baseline.** `judge_baseline.json` holds a committed reference run (FastAPI `0.115.6`,
`gpt-4o-mini` as both generator and judge) to diff future runs against. Refresh it at
full per-step fidelity whenever the pipeline/prompts/model change:

```bash
uv run python -m eval.run_tour_judge_eval --out eval/judge_baseline.json
```

Scores wiggle run-to-run even at `temperature=0`, so watch the trend across runs rather
than gating on a single number. The current baseline aggregate:

| Dimension | Avg |
|---|---|
| Faithfulness | 4.95 |
| Relevance | 4.81 |
| Completeness | 3.67 |
| Ordering | 4.33 |
| Overall | 4.44 |

Completeness is the weakest dimension — expected, since grounding-by-construction makes
faithfulness easy while topic *coverage* is the hard part. It's the number to watch.

## Retrieval Results (FastAPI 0.115.6, k=5)

| Metric | Baseline | Shipped (exp1+3+4+5) | +exp6 BGE rerank (optional) |
|---|---|---|---|
| Hit rate@5 | 0.800 | **0.900** | **0.950** |
| Recall@5 | 0.767 | **0.858** | **0.925** |
| MRR | 0.649 | 0.766 | 0.817 |

The whole repo is ingested (including `tests/` and `docs_src/`) to match production
ingest behavior; those paths are the main source of misses, crowding out library
internals for several queries. The optional BGE reranker closes q17, leaving q03 as
the remaining miss in the current golden set.
