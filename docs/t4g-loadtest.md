# t4g.small load test: findings and deploy decision

**Date:** 2026-09-17 · **Verdict: t4g.small passes. Deploy with 3 workers (`WORKER_MEM=490m`).**

Before deploying to a single EC2 t4g.small (2 vCPU / 2 GB / no swap, ~$19/mo) running
API + job workers via Docker Compose, we simulated the instance locally to answer:
can ~5 users submit ingestion jobs simultaneously without OOMing or bottlenecking the box?

## Method

The harness (committed alongside this doc):

- `docker-compose.t4g.yml` — overlays per-container `cpus`, `mem_limit`, and
  `memswap_limit == mem_limit` (no swap → breaching RAM OOM-kills, matching the real box)
  on the base compose file. API on host port 8001. Requires `DATABASE_URL` (Neon) exported.
- `run_t4g_test.sh` — one run: enqueues 5 ingest jobs, samples `docker stats` (2 s),
  probes API latency on `GET /openapi.json` (2 s), watches `docker events` for OOM kills,
  logs job transitions until drained. Output in `loadtest-results/<timestamp>/`.
- `Backend/scripts/loadtest_enqueue.py` / `loadtest_watch.py` — enqueue via the app's own
  `enqueue_job()` (bypasses Clerk auth), watch the jobs table.

Dev machine is arm64 (same architecture family as Graviton), the database is the real
production Neon instance, and jobs are I/O-bound on OpenAI/GitHub — so the simulation is
faithful except for burst-credit mechanics, which we bracket with a 0.4-CPU run
(the t4g sustained baseline once credits are exhausted).

Five distinct repos per run (small → large): timlrx/tailwind-nextjs-starter-blog,
jballo/camino, jballo/nous-core, confident-ai/deepeval, firecrawl/firecrawl.
Neon test tables truncated between runs so every run does full ingestion.

## Results

Zero OOM kills in all four runs. Peak API-latency probe across ~90 min of testing: **32 ms**
(budget was 500 ms) — the API event loop never blocked.

| Run | Config | 5th job queue wait | Total drain | Peak RAM (workers + api) |
|---|---|---|---|---|
| 1 | 1 worker, 640 MiB / 0.75 CPU | 12.4 min | ~17.5 min | ~475 MiB |
| 2 | 2 workers (planned launch config) | 6.0 min | ~17 min | ~750 MiB |
| 3 | 2 workers @ 0.4 CPU (credits exhausted) | 9.2 min | ~24 min | ~740 MiB |
| 4 | 3 workers @ 490 MiB | **80 s** | ~15 min | ~1.0 GiB |

Per-job detail lives in `loadtest-results/<timestamp>/jobs.log` for each run.

## Findings

1. **RAM does not scale with repo size.** Every worker peaked at ~290 MiB regardless of
   what it ingested — ingestion streams in 256-chunk waves rather than holding the repo
   in memory. Even three large repos ingesting simultaneously (run 4) used ~1.0 GiB of
   the 1.8 GiB container budget. No ingestion semaphore or instance resize is needed.
2. **The box is CPU-bound during chunking.** Total drain time is ~15–17 min in every
   full-CPU config; extra workers cut queue waits dramatically but not total throughput,
   because 2 vCPU saturate. If total throughput ever matters, the fix is in-process async
   job concurrency in the worker loop (claim N under a semaphore + `asyncio.gather`),
   not bigger hardware.
3. **Burst-credit exhaustion degrades gracefully.** At the 0.4-CPU sustained baseline,
   run times stretch uniformly ~1.5× with no cliff, no timeouts, no OOM.
4. **Worker count is purely a queue-wait knob.** 1 → 2 → 3 workers takes the worst-off
   user's wait from 12.4 min → 6 min → 80 s on identical hardware.

## Decision

Launch on t4g.small with `--scale worker=3` and `WORKER_MEM=490m`
(512 MiB API + 3 × 490 MiB workers ≈ 1.98 GiB nominal caps; ~1.0 GiB actual peak,
~800 MiB real headroom). Revisit only if job volume grows past what a 15-min full-queue
drain supports — and then via in-worker concurrency first (finding 2).

## Gotchas for whoever reruns this

- `psycopg2` cannot build in slim uv images (no `pg_config`); the project now depends on
  `psycopg2-binary` for this reason. Don't revert it without adding libpq to the image.
- Stop host dev servers before a run: any process loading `Backend/.env` (worker *or*
  `fastapi dev`) talks to the same Neon queue and steals or interferes with test jobs.
- Never SIGKILL a worker mid-job: it leaves an `idle in transaction` session on Neon that
  blocks `TRUNCATE` until `pg_terminate_backend()`. Stop containers gracefully.
- The worker nulls `claimed_at` when releasing a finished job; `loadtest_watch.py`
  snapshots it mid-run to compute the timing table.
- To rerun the matrix: see "How to run" comments at the top of `docker-compose.t4g.yml`
  and `run_t4g_test.sh`.
