# Storage capacity plan: halfvec embeddings + RDS migration

**Date:** 2026-09-17 · **Status: proposed — phase 1 gated on the retrieval eval.**

The t4g.small load test ([t4g-loadtest.md](t4g-loadtest.md)) settled compute: RAM is
flat at ~290 MiB/worker and the box is CPU-bound. The binding capacity constraint is
elsewhere — **database storage** — and this doc records the plan to lift it.

## Problem

Measured on the production Neon instance after one load-test run (5 repos, 23,309
chunks):

| Table | Size |
|---|---|
| `code_chunk_embeddings` | 370 MB |
| `code_chunks` | 47 MB |
| everything else (jobs, users, follows, connections, …) | **< 0.5 MB** |

That is **~18 KB per chunk all-in** (a 1536-dim float32 vector is 6 KB before HNSW
index and row overhead — the search index outweighs the code it indexes ~8:1), against
Neon's free-tier limit of 0.5 GB. Consequences:

- Total corpus capacity is ~28k chunks — barely more than one load-test run.
- `ingest_max_chunks` (25,000) is effectively "one repo may consume the entire
  database". A vLLM-class repo (~25–30k chunks, ~0.5 GB) cannot fit at all (#52).
- The product goal is orienting contributors in **large** OSS repos, which are exactly
  the repos the ceiling excludes.

Two facts shape the fix. First, 99.9% of the database is a rebuildable cache:
chunks + embeddings regenerate from GitHub + OpenAI for ~$0.25 and a few minutes per
repo. The irreplaceable data (users, GitHub connections, follows, jobs including brief
artifacts) fits in half a megabyte. Second, per-GB storage is cheap on every provider —
the 0.5 GB wall is a free-tier artifact, so the plan is: shrink the footprint, then
move to a small managed instance whose durability protects the half-megabyte that
matters.

## Phase 1 — halfvec (gated)

Store embeddings as pgvector `halfvec(1536)` (2-byte floats) instead of
`vector(1536)`, with an `halfvec_cosine_ops` HNSW index. Roughly halves embedding
storage → ~10 KB/chunk all-in.

**Gate:** the retrieval eval harness (`Backend/eval/run_eval.py`, 20-question FastAPI
0.115.6 golden set). Shipped-stack baseline: **hit@5 0.900 / recall@5 0.858 /
MRR 0.766**. Adopt halfvec only if metrics stay within run-to-run jitter (±0.01 MRR)
with no newly missed questions. Log the run in `eval/EXPERIMENTS.md` as the next
experiment in the (currently paused) retrieval log.

Optional second lever, measured in the same experiment: truncating to 768 dimensions
(`dimensions=768` on the `text-embedding-3-small` call) halves storage again
(~6 KB/chunk, ~4× total). Higher risk of quality loss than halfvec, so it is adopted
only if metrics hold within ~0.02 MRR; otherwise ship halfvec alone. Note a dimension
change invalidates every stored vector — acceptable, the corpus is a cache.

Implementation notes: `EMBED_DIMENSIONS` and the column type live in
`Backend/app/services/embeddings.py` and `Backend/app/models/code.py`; the
pgvector-python SQLAlchemy package provides the `HALFVEC` type. The eval procedure is
schema variant → `ingest_local.py` re-ingest → `run_eval.py` compare.

## Phase 2 — RDS migration

**Target: RDS PostgreSQL, `db.t4g.micro`, single-AZ, 20 GB gp3, same AZ as the EC2
app box. ~$14/month on-demand** ($11.68 instance + $2.30 storage), ~$8–9/month on a
1-year no-upfront reserved instance once validated. pgvector 0.8.x (halfvec included)
is supported on current RDS PostgreSQL versions.

Why this and not the alternatives:

- **Neon paid:** usage-priced compute never scales to zero here — the worker queue
  polling keeps the database awake 24/7, ≈$19/month at the minimum CU before storage.
  Serverless pricing rewards spiky workloads; ours is the opposite.
- **Self-hosted Postgres on the app box:** cheapest (~$2–8/month EBS), but the 2 GB
  box is fully budgeted (512 MiB API + 3×490 MiB workers), and backups/patching/
  restore-testing become manual. Now that the tool is in real use, the ops time is
  worth more than the ~$6/month difference.
- **Skipped outright:** Multi-AZ (2× cost to protect a rebuildable cache), Aurora
  Serverless v2 (same polling tax, ~$43/month floor), provisioned IOPS (trivial query
  volume).

Capacity after phase 1: 20 GB ≈ 2M chunks (halfvec) to 3M+ (halfvec + 768) — the
storage ceiling stops being a product constraint. The practical limit becomes the
1 GB instance RAM for hot HNSW index residency: roughly 10+ large repos hot, colder
repos pay disk latency on first query. Upgrade trigger is observed search latency,
not storage; next step would be `db.t4g.small` (~$26/month) or Supabase Pro ($25 flat,
8 GB) if leaving AWS is acceptable.

Migration is small because the vectors don't move: `pg_dump` the < 0.5 MB of
relational tables into RDS, `CREATE EXTENSION vector;`, apply the phase-1 schema,
point `DATABASE_URL` at RDS, re-ingest followed repos (a few dollars total), keep the
Neon free project untouched during cutover as rollback.

## Unlocked follow-ups

- Raise `INGEST_MAX_CHUNKS` past 25k so vLLM-class repos fit — after the #52
  pre-flight check lands, so over-cap repos still fail before embedding spend.
- Nightly relational-tables backup (kilobytes to S3) can start **now**, on Neon,
  independent of both phases; RDS automated snapshots + PITR supersede it later.
