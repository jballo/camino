# Storage capacity plan: halfvec embeddings + RDS migration

**Date:** 2026-09-17 · **Status: phase 1 implemented: `halfvec(1536)` with no ANN index is the default.**

> **Historical note (2026-09-24):** The Neon capacity problem and cutover framing
> below describe the system before Neon was retired on 2026-09-23. Development now
> uses local Postgres, and the future RDS database will start fresh.

The t4g.small load test ([t4g-loadtest.md](t4g-loadtest.md)) has settled compute under
the shared CPU/memory budget: workers hold ~290 MiB, and the RAM and API-latency gates
pass reproducibly across repeated full-ingestion runs. The one open compute caveat is
drain time against Neon, which is bounded (159 s to the historical 15–17 min) but not
yet measured. The binding capacity constraint is elsewhere — **database storage** is
already demonstrated, and this doc records the plan to lift it. (Changes that would
invalidate the per-worker RAM observation — in-worker concurrency, wave size, and local
model weights — are listed in t4g-loadtest.md § "When the RAM verdict expires".)

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

## Phase 1 — halfvec exact scan (selected)

Exp7 selected **V2: `halfvec(1536)` with no ANN index**. The production-shaped
filtered query did not use HNSW in either the fp32 or halfvec schema, so exact scan
preserves retrieval quality while removing pure storage overhead.

V2 exactly matched the V0 local baseline: **hit@5 0.900 / recall@5 0.858 / MRR
0.766**, with the same q03/q17 misses. SQL latency was **4.24 ms p50 / 5.35 ms
p95** on the ~5.1k-chunk FastAPI fixture.

The former ×4–6 extrapolation is now replaced by direct measurement (exp8
Stage C, 2026-09-23: 50-iteration production-shaped exact scans on the local
testdb, eight live repo/refs, 51,059 chunks total):

| repo@ref | live chunks | p50 | p95 |
|---|---:|---:|---:|
| pallets/flask@main | 1,622 | 2.08 ms | 2.36 ms |
| firecrawl/firecrawl@v2.11.0 | 4,409 | 8.11 ms | 10.51 ms |
| jballo/nous-core@main | 4,799 | 10.91 ms | 18.62 ms |
| tiangolo/fastapi@0.115.6 | 5,129 | 3.31 ms | 4.73 ms |
| fastapi/fastapi@master | 5,686 | 3.72 ms | 4.87 ms |
| firecrawl/firecrawl@main | 6,074 | 10.13 ms | 11.46 ms |
| confident-ai/deepeval@python-v4.2.4 | 11,598 | 15.08 ms | 16.43 ms |
| confident-ai/deepeval@main | 11,742 | 14.11 ms | 16.09 ms |

The largest single ref — what the repo-filtered gate is about — measures
**16.09 ms p95 at 11,742 chunks**, ~15× under the 250 ms gate. Scaling is
roughly linear at ~0.7–2.3 µs/chunk (the spread tracks repo/chunk
characteristics, not noise), so a vLLM-class 25–30k-chunk repo projects to
under ~70 ms p95 even at the worst observed per-chunk rate. Production
telemetry after cutover remains a sanity check, not a blocker.

Measured footprint fell from **553 MB (fp32 + 270 MB HNSW)** to **140 MB**:
74.7% smaller and 3.95× the capacity. Halfvec + HNSW was 275 MB, confirming that
the unused halfvec index alone still cost 135 MB.

The 768- and 512-dimension levers are both **conclusively rejected** by exp8
(Stages A and B, 58 pooled questions across three corpora): 512's apparent
hybrid gain was a fusion artifact masking a real vector-retriever loss
(pooled vector-only ΔMRR@5 −0.037, 95% CI [−0.078, −0.004]), and it failed
every adoption gate. `halfvec(1536)` is the final phase-1 configuration; see
`Backend/eval/EXPERIMENTS.md` (EXP 8-A/8-B/8-C).

Implementation notes: `halfvec(1536)` and no ANN index are now the application
defaults. Fresh databases boot directly into the final schema, including `PLAIN`
column storage. Existing databases are never auto-migrated or auto-dropped; the
startup guard fails fast when their embedding type disagrees with configuration.
No production migration is needed because Neon was retired on 2026-09-23 and the
future RDS database will start fresh.

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

Capacity after phase 1: 20 GB ≈ 2M chunks with halfvec — the storage ceiling stops
being a product constraint. The practical limit becomes the 1 GB instance RAM
available for heap residency during exact scans (about 3.2 KB per embedding row);
cold pages pay disk latency on first query. Upgrade trigger is observed search
latency, not storage; next step would be `db.t4g.small` (~$26/month) or Supabase Pro
($25 flat, 8 GB) if leaving AWS is acceptable.

RDS will start fresh rather than receive a vector migration. On first boot the app
creates the `vector` extension and phase-1 schema; then point the deployment at RDS
and re-ingest followed repos (a few dollars total).

## Unlocked follow-ups

- Raise `INGEST_MAX_CHUNKS` past 25k so vLLM-class repos fit — after the #52
  pre-flight check lands, so over-cap repos still fail before embedding spend.
- Nightly relational-table backups can start with the active database independently
  of the RDS move; RDS automated snapshots + PITR supersede them later.
