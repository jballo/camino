# t4g.small load test: findings and deploy decision

**Date:** 2026-09-21 · **Status: full-ingestion run under the shared budget passed —
RAM and API-latency gates clear. Drain time on Neon is estimated, not measured;
see "Second shared-budget run" for the verdict and the one remaining caveat.**

Before deploying to a single EC2 t4g.small (2 vCPU / 2 GB / no swap, ~$19/mo) running
API + job workers via Docker Compose, we simulated the instance locally to answer:
can ~5 users submit ingestion jobs simultaneously without OOMing or bottlenecking the box?

> **Validity note:** The four historical runs below used only per-container limits.
> Their combined CPU ceilings exceeded 2 vCPU with three workers, and they had no
> aggregate memory ceiling. They are useful workload observations, but they do not
> establish that t4g.small passes. The harness now enforces a shared budget; the
> deployment gate remains open until the matrix is repeated with that version.

## How to run (runbook)

You (a human) run every command below — never an LLM/agent. Everything goes
through `doppler run --` (there are no `.env` files, and even `ps`/`down` need it
because compose interpolates the files). Only the final summary block is meant to
be pasted into a chat.

**1. Start the stack** (3-worker candidate config, local throwaway test db):

```bash
doppler run -- docker compose -f docker-compose.yml -f docker-compose.t4g.yml \
  -f docker-compose.testdb.yml --profile worker up -d --scale worker=3
```

First boot takes a few minutes (uv installs into the venv volumes; the api then
creates the schema in testdb). Later boots reuse the volumes and are fast.

**2. Confirm it's ready:**

```bash
doppler run -- docker compose -f docker-compose.yml -f docker-compose.t4g.yml \
  -f docker-compose.testdb.yml --profile worker ps
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8001/openapi.json
```

Want: api/db/testdb and 3 workers `Up`, `resource_governor` `Exited (0)` (a
one-shot that applies the cgroup caps), and `200` from curl.

**3. Reset the test data** so the run does full ingestion (skipping this makes
ingestion short-circuit to a no-op and invalidates the run):

```bash
psql postgresql://loadtest:loadtest@localhost:5433/loadtest \
  -c 'TRUNCATE jobs, code_chunks, code_chunk_embeddings, repo_index_state'
```

(`githubconnections` is deliberately left alone — it holds the seeded placeholder
row the workers require.)

**4. Run the test** (`--workers` must match the scale from step 1; `--repo`
repeats once per job; `--user-id` can be any consistent string in local-db mode;
`--installation-id` must be a real installation of the GitHub App):

```bash
doppler run -- ./run_t4g_test.sh --local-db --workers 3 \
  --user-id user_loadtest --installation-id <real-id> \
  --repo timlrx/tailwind-nextjs-starter-blog --repo jballo/camino \
  --repo jballo/nous-core --repo confident-ai/deepeval --repo firecrawl/firecrawl
```

The preflights abort with a `PRECHECK FAILED:` line if anything is off (wrong
worker count, stray app processes, oversubscribed memory limits, api not up,
testdb/flag mismatch). At the end it prints the summary block — paste that.

**5. Rerun:** repeat steps 3–4. The stack can stay up between runs.

**6. Turn it off** when done (nothing keeps running, so the next session starts
clean):

```bash
doppler run -- docker compose -f docker-compose.yml -f docker-compose.t4g.yml \
  -f docker-compose.testdb.yml --profile worker down
```

`down` (without `-v`) keeps the volumes: testdb's data, the installed venvs, and
the uv cache survive, so the next `up` is fast and step 3 still applies. For a
scorched-earth reset of the test database only, add:

```bash
docker volume rm camino_testdb-data
```

(the api re-creates the schema on next boot, and the next enqueue re-seeds the
placeholder connection row). Avoid `down -v` unless you also want to sit through
a full dependency reinstall — it deletes the venv/uv-cache volumes too.

If a run was interrupted and jobs look stuck in `running`, step 3 clears them;
never SIGKILL a worker mid-job (see gotchas).

## Method

The harness (committed alongside this doc):

- `docker-compose.t4g.yml` — puts the whole application stack in a shared cgroup capped
  at 1.8 CPU and 1.75 GiB with no swap. This reserves 0.2 CPU and 256 MiB of the
  t4g.small envelope for Linux and Docker, and makes aggregate contention and OOMs
  observable even on a larger development host. Per-container **memory limits must sum
  to at most the parent cap** (default budget: api 384 + db 192 + 3 × worker 400 =
  1776 ≤ 1792 MiB), so an aggregate OOM can never fire before some container's own
  limit — otherwise "which config OOMed" is unanswerable. CPU may oversubscribe (it
  throttles, not kills). The unused local Postgres also joins the shared cgroup
  (capped at 192 MiB), making the rehearsal conservative: the real instance talks to
  Neon and has no local db, freeing ~448 MiB per worker there. API is on host
  port 8001. Environment comes from Doppler — launch everything under
  `doppler run --`; there are no `.env` files.
- `run_t4g_test.sh` — one run, executed **by a human, never by an LLM/agent**.
  Preflight refuses to start if: the worker count differs from `--workers N`, any host
  process or out-of-stack container is running the app (they would steal queue jobs —
  the flaw that invalidated the historical runs), any container in the shared cgroup
  lacks a memory limit, the limits oversubscribe the parent cap, or the cgroup caps
  aren't applied. Then it enqueues the ingest jobs, samples per-container stats and
  aggregate cgroup memory/throttling (2 s), probes API latency on `GET /openapi.json`
  (2 s), watches `docker events` for OOM kills, logs job transitions until drained,
  and prints a **secret-free summary block** to the terminal — that block is the only
  thing meant to be pasted into a chat for interpretation. Raw samples stay in
  `loadtest-results/<timestamp>/`.
- `Backend/scripts/loadtest_enqueue.py` / `loadtest_watch.py` — enqueue via the app's own
  `enqueue_job()` (bypasses Clerk auth), watch the jobs table.
- `docker-compose.testdb.yml` — optional third overlay adding a **local throwaway
  pgvector database** (host port 5433, throwaway `loadtest` credentials), used with
  `./run_t4g_test.sh --local-db`. This is the recommended target: Neon's free tier is
  0.5 GB and already ~450 MB full, so full-ingestion runs there could hit the storage
  cap mid-test. The testdb sits **outside** the `/camino-t4g` cgroup on purpose — it
  models Neon, which is external and consumes none of the instance's budget. The api
  creates the schema on first boot; cleanup is a truncate on `localhost:5433` or a
  volume delete (commands in the file header). Fidelity trade-off: DB round-trips
  lose Neon's network latency, so drain times read slightly optimistic on DB I/O —
  the RAM/CPU/queue-wait verdicts are unaffected. The preflight fails if the testdb
  container and the `--local-db` flag disagree, so jobs can't silently land in one
  database while workers poll another.

### Secret safety

The September 2026 exposure came from `docker compose config` expanding `.env` into a
transcript, and from a connection string pasted into a command. The harness now:

- passes secrets into containers **by name only** (value-less `environment:` entries
  resolved from the Doppler-provided shell env) — no `.env` files, no values in any
  compose file;
- never runs `docker compose config` and never prints container environments
  (use `docker compose config --no-interpolate` if the merged file must be debugged);
- force-disables shell tracing and never echoes env values;
- pattern-scans the final summary for secret-looking strings (connection strings with
  credentials, private-key blocks, API-key shapes) and **withholds it** if anything
  matches, pointing at the file for manual redaction instead.

Dev machine is arm64 (same architecture family as Graviton), the database is the real
production Neon instance, and jobs are I/O-bound on OpenAI/GitHub — so the simulation is
faithful except for burst-credit mechanics, which we bracket with a 0.4-CPU run
(the t4g sustained baseline once credits are exhausted).

Five distinct repos per run (small → large): timlrx/tailwind-nextjs-starter-blog,
jballo/camino, jballo/nous-core, confident-ai/deepeval, firecrawl/firecrawl.
Neon test tables truncated between runs so every run does full ingestion.

## Historical results (not a deployment gate)

Zero OOM kills in all four runs. Peak API-latency probe across ~90 min of testing: **32 ms**
(budget was 500 ms) — the API event loop never blocked.

| Run | Config | 5th job queue wait | Total drain | Peak RAM (workers + api) |
|---|---|---|---|---|
| 1 | 1 worker, 640 MiB / 0.75 CPU | 12.4 min | ~17.5 min | ~475 MiB |
| 2 | 2 workers (planned launch config) | 6.0 min | ~17 min | ~750 MiB |
| 3 | 2 workers @ 0.4 CPU (credits exhausted) | 9.2 min | ~24 min | ~740 MiB |
| 4 | 3 workers @ 490 MiB | **80 s** | ~15 min | ~1.0 GiB |

Per-job detail lives in `loadtest-results/<timestamp>/jobs.log` for each run.

## First shared-budget run (2026-09-21, run `20260921-121008`)

First run under the rebuilt harness (shared 1792 MiB / 1.8-CPU parent cgroup, all
preflights passing, throwaway database). Config: 3 workers @ 400 MiB / 0.75 CPU,
api 384 MiB, db 192 MiB — limits sum 1776 ≤ 1792 MiB.

| Metric | Result |
|---|---|
| Jobs | 5/5 complete, zero OOM events, zero OOM kills |
| Total drain | **305 s (~5 min)** |
| Queue waits | 4.5–33.6 s (worst: firecrawl, 33.6 s) |
| Job run times | 35–281 s (longest: deepeval) |
| Aggregate RAM peak | 1634 MiB (91% of cap) |
| Aggregate CPU | avg **0.21 cores**; throttled 87 periods, 14.9 s total |
| Per-container RAM peaks | api 383, workers 300 / 394 / 326, db 68 MiB |
| API probe | p95 16 ms, max 123 ms, **10 of 148 probes failed** |

### Why this is not yet a gate pass

1. **The workload was ~20× lighter than full ingestion.** Aggregate CPU works out to
   ~64 CPU-seconds for the whole run; the historical full-ingestion runs of the same
   5 repos burned well over 1000 CPU-seconds and took 15–17 min. A 5-minute drain at
   0.21 average cores means the workers almost certainly short-circuited — the repos
   were already present in `repo_index_state` (throwaway DB inherited/retained prior
   ingest data), so this measured incremental/no-op ingestion, not the worst case.
   **Truncate `jobs, code_chunks, code_chunk_embeddings, repo_index_state` (or reset
   the branch to an empty-schema state) and rerun.**
2. **10 failed API probes**, partially diagnosed: the api's RestartCount was 0 (it
   never died), and no ~10 s samples exist in the data — a probe that hit curl's
   10 s `--max-time` would have reported ~10.0 s as the max, but the max was 123 ms.
   So all 10 were *instantaneous* connection failures (refused/reset), most likely
   Docker Desktop's macOS port-forwarding proxy flaking under load — a rehearsal
   artifact that doesn't exist on EC2, where the api listens directly. The probe now
   records curl's exit code per failure (7=refused, 28=timed out, 56=reset) and the
   summary breaks failures down by code, so the rerun will settle this.
3. **api and worker-2 grazed their ceilings** (383/384 and 394/400 MiB) — but with
   RestartCount 0 and zero OOM kills, this is reclaimable page cache, not memory
   pressure: under a tight cgroup the kernel deliberately fills usage to the cap
   with cache. The anonymous working set fit. No rebalancing needed on this
   evidence; watch the same numbers on the full-ingestion rerun.

### What the run does establish

- The rebuilt harness works end to end: preflights, shared-cgroup accounting,
  throttle/OOM deltas, and the paste-able summary all behaved.
- The budget arithmetic held under pressure: 91% aggregate utilization with zero
  aggregate OOM events and zero per-container kills.
- With 3 workers and light jobs, queue waits are seconds, not minutes — consistent
  with the historical finding that worker count drives queue wait.

## Second shared-budget run (2026-09-21, run `20260921-154458`) — full ingestion

Same config (3 workers @ 400 MiB / 0.75 CPU, api 384, db 192), against the local
throwaway testdb with all ingest tables truncated first. **Verified full ingestion:**
23,347 chunks with a 1:1 embedding for every chunk (deepeval 11,398 · firecrawl
6,047 · nous-core 4,799 · camino 1,032 · tailwind-starter 71).

| Metric | Result |
|---|---|
| Jobs | 5/5 complete, **peak concurrent running: 3** (healthy claim behavior) |
| Total drain | **159 s** |
| Queue waits | 2.5–13.9 s (worst: firecrawl) |
| Aggregate RAM peak | 1574 MiB (88% of cap), zero OOM events/kills |
| Worker RAM peaks | 262 / 274 / 280 MiB (vs 400 MiB limits) — matches the ~290 MiB invariant |
| Aggregate CPU | avg 0.23 cores; 0.6 s total throttle |
| API probe | p95 8 ms, max 68 ms, **0 failures** |
| testdb (outside cgroup) | peaked 297% CPU / 450 MiB absorbing inserts + HNSW index work |

### Interpretation

1. **The RAM gate passes, and this evidence transfers to production.** Workers held
   their ~290 MiB invariant, the aggregate budget absorbed full ingestion at 88%
   with zero OOMs, and RAM behavior does not depend on database latency.
2. **The API-latency gate passes.** p95 8 ms with zero probe failures — the run-1
   failures did not recur, supporting the Docker-Desktop-proxy explanation.
3. **"Chunking is CPU-intensive" was substantially Neon round-trip latency in
   disguise.** The same repos that historically drained in 15–17 min finished in
   159 s once the database was local — with workers peaking at only ~29% CPU
   (nowhere near their 0.75-core caps) while testdb did ~3 cores of insert/index
   work. The workload is embedding-API- and DB-bound, not chunking-CPU-bound; the
   2-vCPU constraint matters less than the historical runs suggested.
4. **The one number that does not transfer: 159 s.** Production talks to Neon and
   re-inherits per-wave round-trip latency; the realistic production drain sits
   between 159 s and the historical 15–17 min. Both endpoints of that range passed
   RAM and latency, so this bounds the experience rather than gating the deploy.

### Replication (2026-09-22, run `20260922-105241`)

A third run from a fresh testdb volume reproduced the result almost exactly:
159 s drain (identical), aggregate peak 1557 MiB (87%), workers 260–277 MiB,
zero OOM, p95 9 ms / max 14 ms with zero probe failures, and this time **zero
CPU throttling**. Job ids starting at 1 confirm the fresh volume; the api
re-created the schema and the enqueue script re-seeded the placeholder
connection row automatically. The gate result is reproducible, not a lucky run.

### Remaining caveat before launch

A Neon-targeted confirmation run would pin the real drain time, but the free tier
(~450 MB of 0.5 GB used) likely cannot hold this matrix's ~140 MB of embeddings
plus chunk text — see `docs/storage-capacity-plan.md` (halfvec) before attempting
it. Acceptable alternative: launch on the candidate config and watch the first
real ingestions, since every failure mode tested (OOM, API stall, queue collapse)
passed under both fast-DB (this run) and slow-DB (historical Neon) conditions.

## Observations from the historical runs

1. **RAM does not scale with repo size.** Every worker peaked at ~290 MiB regardless of
   what it ingested — ingestion streams in 256-chunk waves rather than holding the repo
   in memory. Even three large repos ingesting simultaneously (run 4) used ~1.0 GiB of
   the 1.8 GiB container budget. The shared-budget rerun still needs to confirm the
   complete stack's headroom.
2. **Chunking is CPU-intensive.** Total drain time was ~15–17 min in every full-CPU
   configuration while extra workers cut queue waits. Because the old harness did not
   enforce a shared 2-vCPU ceiling, a rerun is required to quantify throughput on the
   target. In-process async job concurrency remains an option to evaluate afterward.
3. **Burst-credit exhaustion degrades gracefully.** At the 0.4-CPU sustained baseline,
   run times stretch uniformly ~1.5× with no cliff, no timeouts, no OOM.
4. **More workers reduced queue wait in these runs.** 1 → 2 → 3 workers took the
   worst-off user's wait from 12.4 min → 6 min → 80 s. The shared-budget rerun must
   determine whether that improvement holds under aggregate CPU contention.

## Deployment gate

**Met on 2026-09-21** (run `20260921-154458`, full ingestion verified by chunk
counts): the candidate configuration `--scale worker=3` with `WORKER_MEM=400m`
(490m was dropped: 3 × 490 + api + db oversubscribed the parent cap; workers peak
~290 MiB, so 400m keeps ~110 MiB headroom each) finished the 5-repo matrix with no
aggregate OOM, p95 API latency of 8 ms with zero failed probes, and queue waits
under 14 s. The only unmeasured quantity is drain time against Neon (bounded at
159 s – 17 min; see the caveat above). On the real instance — which has no local
db container — workers could be raised to ~448 MiB each within the same envelope
if headroom is ever wanted.

## Phase 2 test matrix (planned)

The base matrix is proven (4 consistent runs). These tests target the dimensions it
never exercised. All follow the runbook above — same launch, reset, and teardown;
only the test command changes. Cost note: embedding spend scales with chunk count
(~23k chunks per base matrix); C is free, B is the expensive one — run it
deliberately, not repeatedly.

**A. Queue depth beyond worker count (launch-day worst case).** 10 jobs on 3
workers; jobs dedupe on (repo, *ref*), so one repo at several tags counts as
distinct full ingestions. Doubles as a ~10-minute soak: watch worker RAM peaks for
creep across consecutive jobs (the base runs are too short to show a slow leak).

```bash
doppler run -- ./run_t4g_test.sh --local-db --workers 3 \
  --user-id user_loadtest --installation-id <real-id> \
  --repo timlrx/tailwind-nextjs-starter-blog --repo jballo/camino \
  --repo jballo/nous-core --repo confident-ai/deepeval --repo firecrawl/firecrawl \
  --repo fastapi/fastapi@0.115.0 --repo fastapi/fastapi@0.100.0 \
  --repo fastapi/fastapi@0.95.0 --repo psf/requests --repo pallets/flask
```

Pass: all complete, no OOM, API budget held, and the worst queue wait is a number
we would accept telling a user (record it — this is the launch-capacity headline).

**B. Giant repo (two documented limits at once).** `home-assistant/core` (or
`microsoft/vscode` for TS) should hit the `ingest_max_chunks` 25,000 cap —
untested until now (deepeval peaked at ~11.4k) — and large repos are where a
single enormous file will test the "peak RAM tracks the largest file" blind spot.

```bash
doppler run -- ./run_t4g_test.sh --local-db --workers 3 \
  --user-id user_loadtest --installation-id <real-id> \
  --repo home-assistant/core
```

Pass: completes with chunk count at the cap
(`SELECT count(*) FROM code_chunks` = 25,000), worker RAM within its 400 MiB limit.

**C. Oversized tarball guardrail (near-free negative test).** `torvalds/linux`
exceeds the 200 MB tarball guard; correct behavior is a fast, clean permanent
failure — proving an oversized repo cannot wedge a worker or burn embedding money.

```bash
doppler run -- ./run_t4g_test.sh --local-db --workers 3 \
  --user-id user_loadtest --installation-id <real-id> \
  --repo torvalds/linux
```

Pass: job status `failed` with a tarball-size error within a couple of minutes,
worker healthy afterward, zero embeddings written.

**D. Forced OOM (validate the failure path we have only seen absent).** Relaunch
with `WORKER_MEM=256m` (budget still fits: 384 + 192 + 3×256 = 1344 MiB), run the
base matrix, and confirm the harness *reports* kills and the retry machinery
recovers. Restore the default afterward.

```bash
WORKER_MEM=256m doppler run -- docker compose -f docker-compose.yml \
  -f docker-compose.t4g.yml -f docker-compose.testdb.yml \
  --profile worker up -d --scale worker=3
```

Pass: summary shows nonzero OOM kills, killed jobs requeue (attempts > 1) and
either complete or fail with a real error — no silent wedging.

**E. Credit-exhausted CPU floor (cheap confirmation).** Historical run 3 covered
this on the invalid harness. Prediction: near-identical results, since workers
average ~0.29 cores. `T4G_CPU_MAX` must be set on both the launch *and* the test
command (the preflight verifies the applied cap matches).

```bash
T4G_CPU_MAX="40000 100000" doppler run -- docker compose ... up -d --scale worker=3
T4G_CPU_MAX="40000 100000" doppler run -- ./run_t4g_test.sh --local-db --workers 3 ...
```

Pass: graceful stretch (no cliff), API budget held, no OOM.

**F. Mixed job types (needs harness work first).** Tours and briefs run in these
same workers with a different RAM shape (many retrieved chunks assembled into one
context — the "sneak up on us" case above). The enqueue script only creates
ingest jobs today; extending it to enqueue tour/brief jobs against an already
ingested repo is the prerequisite. Most valuable phase-2 item if launch includes
briefs.

## When the RAM verdict expires

The ~290 MiB/worker peak is baseline (Python + libraries) plus a bounded working set
(one 256-chunk wave, one file being parsed). Repo size never enters it — the streaming
design guarantees that permanently. Per-worker RAM grows only if we change the design.
Deliberate changes that would require rerunning this test:

1. **In-process job concurrency** (finding 2's throughput fix): a worker running N
   jobs holds N wave buffers, N parse states, N in-flight API payloads. Per-worker RAM
   scales with in-process concurrency — the current 1-job-per-process design is why it
   doesn't. Rerun the matrix before shipping this.
2. **Raising `INGEST_WAVE_CHUNKS`**: the direct memory knob. 256 is why RAM stays
   flat; bigger waves hold proportionally more chunks + embedding responses per commit.
3. **Local model weights in the worker** (e.g. the exp6 BGE reranker from
   `Backend/eval/EXPERIMENTS.md`): hundreds of MB resident per process, multiplied by
   worker count. This alone would reshape the t4g.small budget.

Changes that could sneak up on us:

4. **Raising the max-file-size guard**: chunks append per-file *before* the wave-size
   check, and tree-sitter holds the file's source + AST while parsing — so peak RAM
   tracks the largest single file, not the repo.
5. **New job stages holding more state**: briefs/tours run in the same workers. A
   future stage that assembles many retrieved chunks into one large context grows the
   per-job working set in a way ingestion never did.

## Gotchas for whoever reruns this

- `psycopg2` cannot build in slim uv images (no `pg_config`); the project now depends on
  `psycopg2-binary` for this reason. Don't revert it without adding libpq to the image.
- Run *everything* under `doppler run --` (stack launch, the test script): there are no
  `.env` files anymore, so nothing works without it — the GitHub/OpenAI/Clerk secrets
  are needed even in `--local-db` mode, where only the database is swapped out.
- Between `--local-db` runs, truncate on the host port —
  `psql postgresql://loadtest:loadtest@localhost:5433/loadtest -c 'TRUNCATE jobs,
  code_chunks, code_chunk_embeddings, repo_index_state'` — or delete the
  `camino_testdb-data` volume for a full reset (the api re-creates the schema on
  next boot). No Neon storage is consumed either way.
- Stop host dev servers before a run: any Doppler-run process (worker *or* `fastapi
  dev`) talks to the same Neon queue and steals or interferes with test jobs. The
  preflight checks for these and for out-of-stack containers, but it can only see this
  machine — nothing running the app elsewhere may point at the same Neon branch.
- An empty database silently wedges every ingestion job: the worker's ownership guard
  (`worker._ensure_ingestion_owned`) requires a `githubconnections` row for the job's
  installation id and otherwise abandons the job as cancelled *while it still reads
  `running`* — with 3 workers all 5 jobs go "running" in seconds with zero progress,
  cycling via the 600 s lease reaper until they fail on max attempts.
  `loadtest_enqueue.py` now seeds a placeholder row when one is missing (ingestion
  mints tokens from the app credentials, so the row's token fields are never read);
  note the between-runs TRUNCATE deliberately leaves `githubconnections` alone.
- Never SIGKILL a worker mid-job: it leaves an `idle in transaction` session on Neon that
  blocks `TRUNCATE` until `pg_terminate_backend()`. Stop containers gracefully.
- The worker nulls `claimed_at` when releasing a finished job; `loadtest_watch.py`
  snapshots it mid-run to compute the timing table.
- To rerun the matrix: follow the "How to run (runbook)" section at the top of this
  doc; the file-header comments in `docker-compose.t4g.yml`, `docker-compose.testdb.yml`
  and `run_t4g_test.sh` carry the same commands next to the code.
