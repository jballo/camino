# Phase 1 implementation plan — halfvec experiment matrix (exp7)

**Date:** 2026-09-22 · **Status: complete; V2 selected, cutover pending.** Exp7 chose
`halfvec(1536)` with no ANN index; production cutover remains a separate change.
Follow-up: [exp8-dim-confirmation-plan.md](exp8-dim-confirmation-plan.md) targets the
inconclusive 768/512 truncation result and the extrapolated V2 latency number.

## Context

[storage-capacity-plan.md](../storage-capacity-plan.md) phase 1 proposes switching
`code_chunk_embeddings.embedding` from `vector(1536)` to `halfvec(1536)`, gated on the
retrieval eval. The alternatives research
([embedding-storage-alternatives.md](embedding-storage-alternatives.md)) validated
halfvec but found a likely-better variant: because every search is filtered to one
repo (5k–30k vectors), an **exact scan with no ANN index** may pass the latency budget
— recall is exactly 1.0, and total footprint drops 5× instead of 2.2×. It also found
the 768-dim lever can be evaluated (and even migrated!) without re-embedding, via
pgvector's `subvector()` + the fact that cosine distance is scale-invariant.

So phase 1 becomes a small experiment matrix, run once, logged as **exp7** in
`Backend/eval/EXPERIMENTS.md`:

| Variant | Schema | ANN index | Question it answers |
|---|---|---|---|
| **V0** | today (fp32 + HNSW) | global HNSW | Local-DB baseline + is the HNSW index even used for filtered queries? |
| **V1** | halfvec(1536) | HNSW `halfvec_cosine_ops` | The plan as written — quality parity? |
| **V2** | halfvec(1536) | **none** (exact scan) | Does exact-scan latency fit the budget? |
| **V3** | V1 or V2 schema | n/a (exact scan via `subvector`) | Do 768/512 dims hold quality? (query-time truncation, no re-ingest) |

**Decision rule:** ship **V2** if its latency passes; else ship **V1**. If V3@768
holds within ~0.02 MRR, note it as a stackable follow-up (pure-SQL migration, §7) —
adopt per the storage plan's original gate, don't block phase 1 on it.

## Ground rules (project conventions — do not skip)

- **Only the user runs evals/tests.** Prepare code + exact commands; the user
  executes and reports output back.
- **Never `cat`/`grep`/`cut`/`awk` `.env` files or print env secrets.** Local testdb
  creds (`loadtest:loadtest@localhost:5433/loadtest`) are documented throwaways and
  fine to use in commands.
- All experiment runs go against the **local testdb**, not Neon (Neon free tier is
  ~450 MB used; three schema variants don't fit, and prod data stays untouched until
  the final cutover).
- Quality gate (from storage-capacity-plan.md): vs the **V0 local baseline** (not the
  Neon-measured numbers), hit@5 / recall@5 unchanged, MRR within ±0.01 jitter, **no
  newly missed questions** (compare the per-question MISSES table). For V3: ±0.02 MRR.
- Latency gate for V2 (proposed, user may adjust): vector-retriever SQL **p95 ≤
  250 ms at ~25–30k chunks in the searched repo**. (Query embedding via OpenAI
  already costs ~100–300 ms per search, so this roughly keeps the retriever from
  dominating end-to-end latency.)

## Key files

- `Backend/app/models/code.py` — `CodeChunkEmbedding.embedding`, currently
  `Column(Vector(EMBED_DIMENSIONS))` (pgvector-python also ships `HALFVEC`).
- `Backend/app/services/search.py` — `_vector_search()`: the production-shaped query;
  note `CAST(:embedding AS vector)` appears twice (SELECT rank + ORDER BY).
- `Backend/app/main.py` — startup DDL; `ix_embeddings_hnsw` HNSW index
  (`vector_cosine_ops, m=16, ef_construction=64`) plus other indexes. Fresh DBs get
  schema from here; existing DBs need explicit `ALTER`s (§ per-variant).
- `Backend/app/services/embeddings.py` — `EMBED_MODEL`, `EMBED_DIMENSIONS = 1536`.
- `Backend/eval/` — `ingest_local.py` (clones + ingests FastAPI 0.115.6 fixture),
  `run_eval.py` (metrics + MISSES diagnostics), `EXPERIMENTS.md` (log; exp7 goes
  here), `README.md` (harness docs).
- `docker-compose.testdb.yml` — local `pgvector/pgvector:pg16` on host port 5433.

## Step 0 — setup + V0 baseline

1. Start the local testdb (worker/api profile not needed for eval, but the **API must
   boot once** against the fresh DB so startup DDL creates tables/views/indexes — see
   `Backend/eval/README.md` "Reproduce from a fresh checkout").
2. Verify pgvector version supports halfvec + subvector (needs ≥ 0.7; image should
   ship 0.8.x): `SELECT extversion FROM pg_extension WHERE extname = 'vector';`
3. Ingest the fixture with `DATABASE_URL` pointed at the testdb:
   `uv run python -m eval.ingest_local`
4. Baseline eval run:
   `uv run python -m eval.run_eval --label exp7_v0_fp32_hnsw --out eval/runs/exp7_v0.json`
5. **Diagnostic (new small script, `Backend/eval/explain_vector.py`):** embed one
   golden-set query, then run the exact `_vector_search` SQL under
   `EXPLAIN (ANALYZE, BUFFERS)` with production-shaped params (repo/ref/generation/
   model filters, path filter on, `top_n=60`), print the plan + execution time, and
   loop it ~20× to report p50/p95. Record:
   - Does the plan use `ix_embeddings_hnsw` or a seq scan? (If seq scan: the prod
     index is pure storage cost today — strong evidence for V2, and it means the
     eval baseline already reflects exact-scan quality.)
   - If the index IS used: note that `top_n=60 > hnsw.ef_search=40` (default) caps
     the candidate list at ~40 before join filters — record row count actually
     returned. Optionally repeat with `SET hnsw.ef_search = 100;` and with
     `SET hnsw.iterative_scan = relaxed_order;` (pgvector 0.8) to see if the vector
     retriever is currently candidate-starved on filtered queries.

## Step 1 — code changes (one PR, all variants config-driven)

Make the variant switchable by env so runs don't need code edits in between:

1. **Config** (`Backend/app/config.py` + wherever settings are read):
   - `VECTOR_TYPE` = `vector` | `halfvec` (default `vector` until cutover)
   - `VECTOR_INDEX` = `hnsw` | `none` (default `hnsw`)
2. **Model** (`models/code.py`): pick `Vector(…)` vs `HALFVEC(…)` from `VECTOR_TYPE`
   (import `HALFVEC` from `pgvector.sqlalchemy`).
3. **Search SQL** (`services/search.py::_vector_search`): cast the parameter to the
   configured type — `CAST(:embedding AS halfvec(1536))` when halfvec (both
   occurrences). `<=>` works for both types. Add an optional `vector_dims: int |
   None` parameter (plumbed from `hybrid_search_debug`): when set, order by
   `subvector(e.embedding, 1, :dims) <=> subvector(CAST(:embedding AS <type>), 1, :dims)`
   — no renormalization needed (cosine is scale-invariant). This is the V3 mechanism;
   it will be an exact scan (no matching expression index), which is intended.
4. **Startup DDL** (`main.py`): build the ANN index statement from config —
   `halfvec_cosine_ops` when halfvec, and skip creation entirely when
   `VECTOR_INDEX=none`. Keep `IF NOT EXISTS` semantics; do NOT auto-drop/migrate
   existing columns at startup (explicit migration SQL below instead).
5. **Eval flags** (`eval/run_eval.py`): `--vector-dims N` → passes through to
   `_vector_search`. The config block in the output JSON must record it.
6. **Timing** (`eval/explain_vector.py` from step 0): accept `--dims` too, so V2/V3
   latency comes from the same tool.
7. `SET STORAGE PLAIN` note: after the halfvec retype, run
   `ALTER TABLE code_chunk_embeddings ALTER COLUMN embedding SET STORAGE PLAIN;`
   *before* re-writing rows (it only affects new rows) — for the testdb, set it,
   then `VACUUM FULL code_chunk_embeddings;` or re-ingest. 3,080 B fits an 8 KB page.

Sanity: `uv run pytest` for the touched modules; the existing tests must pass with
defaults unchanged (`VECTOR_TYPE=vector`, `VECTOR_INDEX=hnsw` — zero behavior change
until env flips).

## Step 2 — V1 run (halfvec + HNSW)

On the testdb (explicit migration, no re-embed — fp16 is a lossy but
quality-verified downcast):

```sql
DROP INDEX IF EXISTS ix_embeddings_hnsw;
ALTER TABLE code_chunk_embeddings
  ALTER COLUMN embedding TYPE halfvec(1536) USING embedding::halfvec(1536);
ALTER TABLE code_chunk_embeddings ALTER COLUMN embedding SET STORAGE PLAIN;
VACUUM FULL code_chunk_embeddings;
CREATE INDEX ix_embeddings_hnsw ON code_chunk_embeddings
  USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
```

Then with `VECTOR_TYPE=halfvec`:
- `uv run python -m eval.run_eval --label exp7_v1_halfvec_hnsw --out eval/runs/exp7_v1.json`
- `explain_vector.py` → plan + p50/p95.
- Record table + index sizes:
  `SELECT pg_size_pretty(pg_total_relation_size('code_chunk_embeddings'));` and
  `SELECT pg_size_pretty(pg_relation_size('ix_embeddings_hnsw'));`

## Step 3 — V2 run (halfvec, no ANN index)

```sql
DROP INDEX IF EXISTS ix_embeddings_hnsw;
```

With `VECTOR_TYPE=halfvec VECTOR_INDEX=none`:
- `uv run python -m eval.run_eval --label exp7_v2_halfvec_noindex --out eval/runs/exp7_v2.json`
- `explain_vector.py` p50/p95 — **this is the deciding number.** The fixture is
  FastAPI (~5–8k chunks); exact scan is O(n), so extrapolate ×4–6 for a 30k-chunk
  repo, or (better, optional) bulk-ingest a larger corpus into the testdb — the t4g
  load-test harness already ingests 5 repos / ~23k chunks (`run_t4g_test.sh
  --local-db`) — and re-measure with a real 25k+ repo. Judge against the ≤250 ms p95
  gate at 30k.
- Quality must equal V1-or-better by construction (exact scan); if it doesn't,
  something is wrong — investigate before proceeding.

## Step 4 — V3 runs (dimension truncation, query-time)

On whichever schema is current (works on halfvec or vector; `subvector` supports
both in pgvector 0.7+ — verify once on the testdb before the run):

```bash
uv run python -m eval.run_eval --vector-dims 768 --label exp7_v3_768 --out eval/runs/exp7_v3_768.json
uv run python -m eval.run_eval --vector-dims 512 --label exp7_v3_512 --out eval/runs/exp7_v3_512.json
```

Gate: within ~0.02 MRR of V0 and no newly missed questions (matches the storage
plan's original 768 gate). This measures quality only — the storage win comes later
via §7. (Equivalence of truncation+renormalize to OpenAI's `dimensions` parameter is
established; cosine needs no renormalize at all.)

## Step 5 — log + decide

1. Add **exp7** to `eval/EXPERIMENTS.md`: leaderboard rows for v0/v1/v2/v3_768/
   v3_512 (hit@5, recall@5, MRR), a detail section with the EXPLAIN findings
   (index-used-or-not is a headline result), latency p50/p95 per variant, and
   measured table/index sizes. Update the per-question outcome matrix.
2. Decide per the rule at the top (V2 if latency passes, else V1; V3 noted as
   stackable or rejected).
3. Update `docs/storage-capacity-plan.md` phase 1 with the chosen variant + numbers.

## Step 6 — production cutover (after decision; separate PR)

1. Set `VECTOR_TYPE`/`VECTOR_INDEX` in prod env (Doppler) per the decision.
2. Run the V1-or-V2 migration SQL against Neon (the winning variant's block above).
   `VACUUM FULL` on `code_chunk_embeddings` needs a table-copy's worth of free
   space — with ~450 MB used of 500 MB this may not fit on Neon. Fallback that
   avoids VACUUM FULL entirely: `TRUNCATE code_chunk_embeddings` + re-ingest
   followed repos (corpus is a rebuildable cache, ~$0.25/repo — sanctioned by the
   storage plan), or do the retype as part of the phase-2 RDS cutover.
3. Verify: one production search per followed repo returns sane results; record new
   `pg_total_relation_size`.

## §7 — stackable follow-up if V3@768 passed (do NOT do in phase 1)

Storage-realizing migration without re-embedding, pure SQL:
`ALTER COLUMN embedding TYPE halfvec(768) USING (subvector(embedding, 1, 768))`
(cosine unaffected by the missing renormalization), set `EMBED_DIMENSIONS = 768` and
pass `dimensions=768` in `embeddings.create(...)` for all future embeds, update the
`dimension` column values, re-check `uq_chunk_model` consistency. Log as exp8.

## Out of scope for phase 1 (parked, see alternatives doc)

Binary quantization + rerank (escalation path if more headroom needed),
voyage-code-4 (quality experiment for when the retrieval log unpauses), S3 fp16
shards + in-process brute force (re-decide **phase 2/RDS** against this before
migrating).
