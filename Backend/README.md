# Camino — Backend

FastAPI service behind Camino's open source contribution tool: GitHub issue-brief
generation first, plus the supporting machinery — GitHub App connection storage, repo
ingest, hybrid code search, ask-the-codebase Q&A, guided-tour generation, per-user API
rate limiting, and Clerk account-lifecycle and GitHub installation webhook handling.

**Retrieval loop:** paused at a tuned stack — exp1–5 shipped (hit@5 0.900), plus an
optional exp6 cross-encoder reranker (BGE blend → 0.950). See
[eval/EXPERIMENTS.md](eval/EXPERIMENTS.md).
**Phase 2 status:** the issue-brief backend — the main product surface — is wired end
to end with the frontend home page: GitHub issue preflight, fork/upstream and
contribution-target resolution, ref-aware ingestion dependencies, grounded generation,
polling, listing, and cancellation through `/api/v1/briefs`, all backed by the durable
shared Postgres `Job` queue. The same queue backs asynchronous repository ingestion and
the guided-tour path: the Plan → Retrieve → Draft → Review graph and the
`/api/v1/journeys` create, poll, list, and cancel flow used by `/generate`, `/tours`,
and `/tours/{id}`.

---

## Stack

- **FastAPI** + SQLModel + Postgres with **pgvector** (`halfvec(1536)` exact scans by default)
- **tree-sitter** — Python, JavaScript, TypeScript/TSX symbol extraction
- **OpenAI** — embeddings (`text-embedding-3-small`) + chat (`gpt-4o-mini` default)
- **LangGraph** — ReAct Q&A agent plus structured tour and issue-brief graphs
- **Clerk** — JWT auth on API routes plus signed account-lifecycle webhooks
- **PyGithub** — GitHub App installation tokens for repo access
- **PostgreSQL fixed windows** — atomic, per-Clerk-user limits for costly API operations

---

## Run locally

```bash
# From repo root — start Postgres
docker compose up -d

cd Backend
cp .env.example .env        # fill in secrets (see below)
uv sync
uv run python -m app.migrate  # create or upgrade the schema
uv run fastapi dev app/main.py --port 8000

# In a second terminal
cd Backend
uv run python -m app.worker
```

API docs: http://127.0.0.1:8000/docs

The API does not run jobs in-process by default. As an alternative to the
second terminal, start the supervised Compose worker after filling in
`Backend/.env`:

```bash
docker compose --profile worker up -d worker
```

The Compose worker uses `restart: always`, connects to the Compose Postgres
service, and runs the same `python -m app.worker` entrypoint. The worker profile
also runs a one-off `migrate` service first, so the worker starts on a migrated
schema.

Neither the API nor the worker creates or changes the schema. Both check at startup
that the database is at the migration revision they expect and that its embedding
column matches `VECTOR_TYPE`, and exit with a message otherwise; run
`python -m app.migrate` after every pull that adds a migration. A database the
API created before the migrations existed needs adopting once (see
[Migrations](#migrations)).

Clerk user sync and GitHub App uninstall cleanup arrive via webhooks, which need a
publicly reachable backend. To exercise them locally, expose port 8000 with a tunnel and
configure Clerk to send `user.created`, `user.updated`, and `user.deleted` to
`/webhooks/clerk`, and GitHub to send installation events to `/webhooks/github`.

### Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `DATABASE_POOL_SIZE` | Persistent connections per backend process (default `5`) |
| `DATABASE_MAX_OVERFLOW` | Temporary overflow connections per backend process (default `10`) |
| `DATABASE_CONNECT_TIMEOUT` | Seconds to wait while opening a database connection (default `10`) |
| `DATABASE_KEEPALIVES_IDLE` / `DATABASE_KEEPALIVES_INTERVAL` / `DATABASE_KEEPALIVES_COUNT` | TCP keepalive timing; a dead connection raises after about idle + interval × count seconds (defaults `30` / `10` / `3`) |
| `DATABASE_LOCK_TIMEOUT_MS` | Postgres `lock_timeout`: how long a statement may wait for another session's lock before failing; `0` disables (default `30000`) |
| `OPENAI_API_KEY` | Embeddings + agent chat |
| `AGENT_MODEL` | Chat model (default `gpt-4o-mini`) |
| `VECTOR_TYPE` | pgvector embedding column/query type: `halfvec` (default) or `vector`; must match the existing database column |
| `VECTOR_INDEX` | ANN index mode: `none` (default, exact scan) or `hnsw`; the index is created by hand ([Vector index](#vector-index)), and startup warns when the setting and the database disagree |
| `CORS_ORIGINS` | Allowed browser origins, comma-separated (default `http://localhost:3000`) |
| `CLERK_SECRET_KEY` | Clerk backend API |
| `CLERK_WH_KEY` | Clerk webhook signing secret |
| `CLERK_JWT_KEY` | Optional — local JWT verification |
| `GH_APP_ID` / `GH_APP_CLIENT_ID` / `GH_APP_SECRET` | GitHub App credentials |
| `GH_APP_PRIVATE_KEY` | GitHub App PEM (escaped newlines OK) |
| `GH_WEBHOOK_SECRET` | GitHub webhook verification |
| `RATE_LIMIT_AGENT_ASK_REQUESTS` / `RATE_LIMIT_AGENT_ASK_WINDOW_SECONDS` | Q&A limit (default 20 requests / 600 seconds) |
| `RATE_LIMIT_REPOSITORY_INGEST_REQUESTS` / `RATE_LIMIT_REPOSITORY_INGEST_WINDOW_SECONDS` | Ingest limit, shared by `/ingest` and follows that queue a new ingest (default 2 requests / 3600 seconds) |
| `INGEST_MAX_TARBALL_BYTES` | Maximum compressed GitHub tarball download size (default `209715200`, or 200 MiB) |
| `INGEST_MAX_EXTRACTED_BYTES` | Maximum cumulative expanded archive size (default `1073741824`, or 1 GiB) |
| `INGEST_MAX_ARCHIVE_ENTRIES` | Maximum tar archive member count (default `100000`) |
| `INGEST_WAVE_CHUNKS` | Parsed chunks embedded and persisted per ingestion wave (default `256`) |
| `INGEST_MAX_CHUNKS` | Hard per-repository chunk cap; oversized ingests fail permanently (default `25000`) |
| `RATE_LIMIT_REPOSITORY_SEARCH_REQUESTS` / `RATE_LIMIT_REPOSITORY_SEARCH_WINDOW_SECONDS` | Direct-search limit (default 60 requests / 60 seconds) |
| `RATE_LIMIT_CONTRIBUTION_TARGET_REQUESTS` / `RATE_LIMIT_CONTRIBUTION_TARGET_WINDOW_SECONDS` | Contribution-target discovery limit (default 60 requests / 60 seconds) |
| `RATE_LIMIT_JOURNEY_CREATE_REQUESTS` / `RATE_LIMIT_JOURNEY_CREATE_WINDOW_SECONDS` | Journey creation limit (default 5 requests / 3600 seconds) |
| `RATE_LIMIT_ISSUE_BRIEF_CREATE_REQUESTS` / `RATE_LIMIT_ISSUE_BRIEF_CREATE_WINDOW_SECONDS` | Issue-brief creation limit (default 5 requests / 3600 seconds) |
| `RUN_WORKER` | Start the shared job worker in the API process (default `false`; use only for an explicitly combined deployment) |
| `WORKER_POLL_INTERVAL` | Seconds between empty-queue polls (default `1.5`) |
| `WORKER_LEASE_TIMEOUT` | Seconds before a dead worker's claim is stale (default `600`); active jobs renew their lease every one-third of this interval |
| `WORKER_MAX_ATTEMPTS` | Claims allowed before stale recovery marks a job failed (default `3`) |

## AWS deployment target

The backend will run on **ECS Fargate**, provisioned by the TypeScript CDK app in the
planned `Infrastructure/` directory. PostgreSQL will run on **Amazon RDS** in isolated
subnets. See the "AWS deployment plan" section of the root [README](../README.md) for
stack boundaries and deployment order.

### Backend work required before Fargate

- Done: `Dockerfile` builds the production image (see "Production image" below).
- Done: unauthenticated `GET /health` liveness endpoint with no database or external
  calls. It is left out of the OpenAPI schema. A separate readiness check is not
  needed: startup fails when the database or the embedding schema is unusable.
- Completed: `POST /api/v1/github/connect` verifies `installationId` against the
  authenticated GitHub user's accessible installations before persisting it.
- Revoke or uninstall the external GitHub App authorization when Clerk's confirmed
  account-deletion flow triggers the existing local cleanup service.
- Add explicit request/model deadlines and cap the parsed repository file count before
  generating tours. Compressed tarballs, expanded archive bytes, archive entries, and
  generated chunks are already capped.
- Handle `SIGTERM` so in-flight jobs can finish or be cancelled cleanly; stale
  `running` rows are requeued or failed by the worker's lease recovery.

### Production image

`Dockerfile` builds one arm64 image that runs either the API (default command) or
the worker. The build context is this directory; `.dockerignore` keeps tests, eval
fixtures, `.venv`, and every `.env*` and key file out of the image.

```bash
# from the repository root; tag with the git SHA so a rollback is the previous tag
docker build --platform linux/arm64 -t camino-backend:$(git rev-parse --short HEAD) Backend
```

- Two stages on one base image, `python:3.14.10-slim-trixie` (an exact patch
  version, so the venv always runs on the interpreter it was built with). The build
  stage copies in the uv 0.12.23 binary and runs `uv sync --frozen --no-dev` into
  `/opt/venv`; the runtime stage copies only that venv and the application code, so
  uv never ships. Dependency bytecode is compiled at build time.
- Runs as the non-root user `app` (uid 10001). Code and the venv are owned by root
  and read-only to it.
- API: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
  --forwarded-allow-ips '*' --no-server-header`. Only Caddy reaches port 8000 and it
  is never published from the host, which is why every proxy peer is trusted.
- Worker: override the command with `python -m app.worker`.
- Migrations: override the command with `python -m app.migrate` and run it once
  before starting the API and worker; neither creates the schema.
- Configuration is environment variables only (names in `.env.example`); nothing is
  baked in. `RUN_WORKER` stays `false` in the API container.
- `docker stop` sends `SIGTERM`: the worker exits 0 after its loop stops; uvicorn
  finishes its shutdown and then re-raises the signal, so its exit code is 143.
  Both are normal. Run containers with `init: true` so signals are forwarded.

### Migrations

Alembic owns the schema. `python -m app.migrate` (`app/migrate.py`) applies every
pending revision in `migrations/versions/` and needs only `DATABASE_URL`. In
production it runs as the one-off `migrate` service before the API and worker start;
[docs/deploy.md](../docs/deploy.md#migrations) covers lock timeouts, retries, and
rollbacks.

```bash
uv run python -m app.migrate            # upgrade to the newest revision
uv run python -m app.migrate current    # the database's revision and this code's
uv run python -m app.migrate adopt      # a database the old API startup created
```

The first full revision, `20261029_01`, creates every table, index, and the
`live_code_chunks` view on an empty database: `halfvec(1536)` embeddings with `PLAIN`
storage and no HNSW index. Never edit a revision once it is merged; change the schema
with a new one:

1. Change the models, then generate a draft against a database at the newest
   revision with `uv run alembic revision --autogenerate -m "<what changes>"`. Review
   it by hand: autogenerate does not see partial index predicates, index methods,
   views, or column storage, and it renders SQLModel string types as
   `sqlmodel.sql.sqltypes.AutoString`, which should become `sa.String`.
2. Keep it compatible with the previous release (add columns nullable or with a
   default; drop or rename only a release after the code stopped using them), so a
   rollback can run the previous release against the new schema.
3. Keep it transactional. A failed or timed-out upgrade rolls back completely and is
   retried as a whole; `op.get_context().autocommit_block()` (for example `CREATE
   INDEX CONCURRENTLY`) would commit part of it, so such a change needs its own
   restartable procedure first.
4. `uv run alembic check` on the migrated database must report nothing, and
   `tests/test_migrations_pg.py` compares the migrated catalog with the models plus
   the view and storage settings, which `alembic check` cannot see.

#### Adopting a database created by the API

Until the baseline, the API created and altered the schema at every startup. Such a
database (the local dev database, the exp7 eval database, or a load-test volume)
already has the tables, so the baseline cannot run on it. Adopt it once instead,
with this release checked out and the API and worker stopped:

```bash
uv run python -m app.migrate adopt     # or under doppler run --, like the app
```

`adopt` compares the database's catalog with the baseline (tables, columns, types,
defaults, constraints, indexes including their predicates, methods, and validity, the
view, and storage) and only when they match stamps it at `20261029_01`, changing no
data. If they differ it prints every difference and stamps nothing: fix them by hand
and run `adopt` again, or recreate the database (`docker compose down -v`, then
`python -m app.migrate`). Recreating loses users, connections, jobs, and briefs; the
code index can be rebuilt by ingesting again. An `ix_embeddings_hnsw` index is
ignored.

Differences an older API startup typically leaves, with their fixes:

```sql
-- "column jobs.refresh_cycles differs ... default=0": the column was added later
ALTER TABLE jobs ALTER COLUMN refresh_cycles DROP DEFAULT;
-- "missing index ix_jobs_ref" (or another ix_jobs_* on a column added later)
CREATE INDEX ix_jobs_ref ON jobs (ref);
-- "... (INVALID: a failed build ...)": an index whose build was interrupted
DROP INDEX <name>;  -- then create it again as the difference shows
```

If the differences include the old token columns on `githubconnections`, the
database missed the September revision; run `uv run alembic upgrade 20260928_01`
first. A database that has not been started since September may still have a
`vector(1536)` column; convert it first:

```sql
DROP INDEX IF EXISTS ix_embeddings_hnsw;
ALTER TABLE code_chunk_embeddings
  ALTER COLUMN embedding TYPE halfvec(1536) USING embedding::halfvec(1536),
  ALTER COLUMN embedding SET STORAGE PLAIN;
ANALYZE code_chunk_embeddings;
```

#### Vector index

Search scans every embedding by default (`VECTOR_INDEX=none`). The HNSW index is not
part of the migrations; with `VECTOR_INDEX=hnsw`, create it by hand:

```sql
CREATE INDEX ix_embeddings_hnsw ON code_chunk_embeddings
  USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
```

Startup warns when `VECTOR_INDEX` and the database disagree, and never creates or
drops the index. The API and worker refuse to start when `VECTOR_TYPE` disagrees with
the embedding column.

### RDS

Before connecting ECS to RDS:

1. Keep schema migration permission separate from the runtime application's normal
   database access where practical; the API and worker need no DDL rights.
2. Run `python -m app.migrate` from the backend image as a one-off task before
   updating the web service.
3. Use an RDS connection URL with TLS enabled, and size
   `DATABASE_POOL_SIZE`/`DATABASE_MAX_OVERFLOW` against the instance's connection budget.

The private alpha can begin with Single-AZ RDS, encrypted gp3 storage, seven-day backups,
and one Fargate web task. RDS must not be publicly accessible; its security group should
accept port 5432 only from the Fargate task security group.

### Secrets and runtime configuration

Inject these values from Secrets Manager into the task definition:

- `DATABASE_URL`
- `OPENAI_API_KEY`
- `CLERK_SECRET_KEY`, `CLERK_WH_KEY`, and `CLERK_JWT_KEY`
- `GH_APP_ID`, `GH_APP_CLIENT_ID`, `GH_APP_SECRET`, `GH_APP_PRIVATE_KEY`, and
  `GH_WEBHOOK_SECRET`

Non-secret settings such as `AGENT_MODEL`, `CORS_ORIGINS`, pool sizes, and rate-limit
thresholds can be plain task-definition environment variables. Secret values must not
be embedded in the Docker image, CDK source, CloudFormation outputs, or committed
`.env` files. Production `CORS_ORIGINS` must include the Vercel frontend origin.

### Job queue

Tour generation, issue-brief generation, and repository ingestion insert typed
`pending` rows in the shared `jobs` table. The standalone polling worker
(`python -m app.worker`) claims the oldest
pending job with `FOR UPDATE SKIP LOCKED` and dispatches it by type. Active duplicate
requests reuse the same row through a status-scoped unique deduplication key. Known
transient upstream and database errors return the job to `pending`; each claim
increments `attempts`, and the job becomes `failed` after `WORKER_MAX_ATTEMPTS`.
Repository names are case-folded for queue, index, and search identity, so casing
variants cannot create competing jobs or generations.

Repository ingestion verifies installation access with PyGithub and rejects repository
metadata marked private, then streams one public GitHub tarball snapshot up to
`INGEST_MAX_TARBALL_BYTES`, safely extracts it, and parses supported source files
locally. Extraction also enforces
`INGEST_MAX_EXTRACTED_BYTES` and `INGEST_MAX_ARCHIVE_ENTRIES` before parsing. The
snapshot reflects a single commit. This blocking download/extract/parse stretch runs
with `asyncio.to_thread`; embedding calls and job orchestration remain asynchronous.

Parsing, embedding, and inserts run in bounded waves of `INGEST_WAVE_CHUNKS`. Each
ingest writes a new generation that remains invisible while its waves commit. Once
complete, one short transaction updates `repo_index_state`, marks the ingest job
complete, and deletes the old rows; failed waves leave the previous complete index live. The
`INGEST_MAX_CHUNKS` cap rejects oversized repositories before further embedding. Each
wave and the final publication revalidate and lock the worker's job ownership, so a
reclaimed or deleted job cannot commit more data. Single-statement reads use the
`live_code_chunks` view; multi-statement hybrid search resolves one active generation
and binds every retrieval and hydration query to it.

One shared ingest job does the work for each repository and ref. It belongs to nobody:
it stores no user or installation. Every requester, including the first, gets their own
waiting row blocked on it, and briefs block on it directly. A worker claims the shared
ingest only while at least one waiting job's owner still has an active GitHub connection,
and runs it under that owner's installation, oldest waiting job first. If GitHub refuses
that installation, the next one is tried. Before each wave commit and the final
publication, the worker checks that someone eligible is still waiting on that
installation; if not, it stops and returns the ingest to `pending` without spending an
attempt. The shared ingest only ends as `complete` or `failed`, and waiting rows take its
outcome. Revoking, suspending, deleting, or reconnecting a user only cancels, deletes, or
moves that user's own rows, so other users waiting on the same ingest are unaffected. A
shared ingest nobody is waiting on stays `pending` until someone requests it again.

Indexes are keyed by normalized repository and ref, so a contribution branch can live
beside another indexed branch without collisions. Search, Q&A, tours, and briefs resolve
one ref and recheck the requesting user's live GitHub access before reading the shared
index; stored visibility is metadata rather than authorization.

Issue-brief creation previews the issue thread, detects contribution warnings and
maintainer branch instructions, resolves fork/upstream identity, and selects a verified
target from contribution docs, pull-request templates, recently merged PRs, or the
default branch. If that repository/ref is not indexed, the brief is parked behind a
deduplicated ingestion job. The worker can also request a bounded refresh when cited
files have changed, then resumes the brief without spending a normal retry attempt.
Brief rows that predate the stored issue-repository identity (`issue_repo` is
null) cannot be resumed; inferring that identity from the upstream repository would
be unsafe for issues opened on forks. Older API startups marked active ones failed.
The migrations do not rewrite job data, so a database adopted with such a row still
active needs it failed or deleted by hand, and the brief recreated.

Multiple processes can share the queue. If a worker dies, lease recovery returns its
row to `pending` (or marks it `failed` at the attempt limit) after
`WORKER_LEASE_TIMEOUT`. The heartbeat runs every one-third of the lease timeout
(200 seconds with the defaults), independently of empty-queue polling; stale recovery
scans every 60 seconds.

Pending or running jobs can be cancelled through their type-specific API endpoint.
Cancellation changes the row to `cancelled` and clears its claim. The heartbeat then
causes in-flight tour or brief generation to cancel its LangGraph task. Cancelling an
ingestion request cancels only the requester's waiting row; when nobody else is waiting,
the running shared ingest stops at its next wave commit and goes back to `pending`. Cancelling a completed or failed
job returns `409`, while cancelling an already-cancelled job is idempotent.

Keep `RUN_WORKER=false` in API processes and supervise the separate worker process.
The local Compose worker uses `restart: always`; production orchestration should apply
the equivalent always-restart policy. `RUN_WORKER=true` remains available for an
explicitly combined local process. Atomic claims make either topology—and multiple
worker processes—safe.

---

## Module layout

```
app/
├── main.py              # FastAPI app, local DB/table initialization, indexes and view
├── db_schema.py         # embedding type validation and stale-index warning
├── worker.py            # Postgres-backed shared job claim/dispatch loop
├── rate_limit.py        # PostgreSQL fixed-window limiter dependencies
├── api/
│   ├── repositories.py  # list repos, enqueue/poll/cancel ingest, hybrid search
│   ├── agent.py         # POST /ask — LangGraph Q&A
│   ├── journeys.py      # create/poll/list/cancel tour generation jobs
│   ├── briefs.py        # preview/create/poll/list/cancel issue briefs
│   └── github.py        # GitHub App OAuth / installation
├── agent/
│   ├── graph.py         # ReAct StateGraph (agent ↔ tools loop)
│   ├── runner.py        # answer_question() entry point
│   └── tools.py         # hybrid_search tool bound per request
├── tour/
│   ├── graph.py         # Plan → Retrieve → Draft → Review graph
│   ├── runner.py        # generate_tour() entry point
│   ├── extract.py       # deterministic snippet/path/line grounding
│   └── review.py        # structural + coverage checks
├── brief/
│   ├── graph.py         # Synthesize → Retrieve → Freshness → Draft → Review
│   ├── runner.py        # generate_brief() entry point
│   └── schemas.py       # structured LLM outputs
├── services/
│   ├── account_deletion.py # transactional, idempotent local account cleanup
│   ├── installation_deletion.py # installation-scoped GitHub webhook cleanup
│   ├── jobs.py          # shared enqueue, deduplication, normalization and cancellation
│   ├── repository_ingestion.py # snapshot, bounded waves and atomic generation publish
│   ├── target_branch.py # contribution-target evidence ladder
│   ├── repo_access.py   # live GitHub authorization for shared indexes
│   ├── issue_thread.py  # issue metadata, comments and preflight warnings
│   ├── fork_status.py   # fork/upstream identity and drift
│   ├── parser.py        # tree-sitter chunk extraction
│   ├── embeddings.py    # build_embedding_text + OpenAI embed
│   ├── search.py        # hybrid search (vector + FTS + RRF)
│   └── search_index.py  # tsvector population SQL
├── models/              # SQLModel tables (users, chunks, embeddings, …)
└── webhooks/            # Clerk + GitHub webhook handlers

eval/
├── golden_dataset.json  # 20 hand-labeled FastAPI questions
├── run_eval.py          # retrieval metrics harness
├── run_agent_smoke_eval.py
├── run_structural_eval.py
├── run_tour_smoke_eval.py
├── run_tour_judge_eval.py   # LLM-as-judge tour scoring
├── judge/               # judge rubric schemas, prompt, call + score reduction
├── judge_baseline.json  # committed tour judge reference run
├── ingest_local.py      # eval ingest from local clone
└── EXPERIMENTS.md       # experiment log + next steps
```

---

## Key API routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/github/connection` | Return the authenticated user's GitHub connection status |
| `POST` | `/api/v1/github/connect` | Exchange GitHub OAuth code and persist encrypted, expiring user-to-server credentials |
| `GET` | `/api/v1/repositories` | List repos for the authenticated user's GitHub installation |
| `GET` | `/api/v1/repositories/overview` | List installed and personally requested repositories with index facts |
| `GET` | `/api/v1/repositories/lookup` | Check an exact `owner/repo` against GitHub and the shared index |
| `POST` | `/api/v1/repositories/follows` | Attach or request a repository for the authenticated user |
| `DELETE` | `/api/v1/repositories/follows/{owner}/{repo}` | Remove a repository from the authenticated user's requested list |
| `GET` | `/api/v1/repositories/contribution-target?repoName=` | Resolve the preferred pull-request target branch and its evidence |
| `POST` | `/api/v1/repositories/ingest` | Queue repository parsing + embedding; returns `{id, status}` |
| `GET` | `/api/v1/repositories/ingest/{id}` | Poll an ingestion job; returns result/error when available |
| `POST` | `/api/v1/repositories/ingest/{id}/cancel` | Cancel an owned pending/running ingestion job |
| `POST` | `/api/v1/repositories/search` | Direct hybrid search (no agent) |
| `POST` | `/api/v1/agent/ask` | Ask the codebase (ReAct agent) |
| `POST` | `/api/v1/journeys` | Queue a guided-tour job for an ingested repo |
| `GET` | `/api/v1/journeys/{id}` | Poll a journey job; returns artifact/error when available |
| `POST` | `/api/v1/journeys/{id}/cancel` | Cancel an owned pending/running tour job |
| `GET` | `/api/v1/journeys?repo=` | List the authenticated user's journey jobs |
| `POST` | `/api/v1/briefs/preview` | Inspect an issue, contribution warnings, upstream/fork state, and target branch |
| `POST` | `/api/v1/briefs` | Queue a grounded issue brief and any required ref ingestion |
| `GET` | `/api/v1/briefs` | List the authenticated user's issue-brief jobs |
| `GET` | `/api/v1/briefs/{id}` | Poll an issue brief, including its dependency phase and artifact/error |
| `POST` | `/api/v1/briefs/{id}/cancel` | Cancel an owned pending/running issue brief |
| `POST` | `/webhooks/clerk` | Process signed Clerk lifecycle events, including account cleanup |
| `POST` | `/webhooks/github` | Process signed GitHub installation events and clean up deleted installations |

All `/api/v1/*` routes require `Authorization: Bearer <clerk_session_jwt>`. The backend
verifies the token and uses its `sub` claim as the sole source of user identity; API
paths and request bodies do not accept `userId`. Legacy user-ID paths return `404`, and
body models reject a legacy `userId` field with `422`.
The Clerk webhook instead requires a valid Svix signature.

POST request bodies:

- GitHub connect: `{ code, installationId }`
- Repository ingest: `{ repoName, ref? }` (omitting `ref` runs contribution-target discovery)
- Repository search: `{ query, repoName, ref?, limit? }` (`limit` defaults to `10`, maximum
  `100`)
- Agent Q&A: `{ question, repoName, ref? }`
- Journey creation: `{ repoName, ref?, topic }`
- Issue-brief preview/create: `{ issueUrl, targetBranch? }`

All three background job types use `pending`, `running`, `complete`, `failed`, and
`cancelled` statuses. A completed ingestion poll includes
`result: {chunks_inserted, embeddings_created}`; failed jobs include `error`, and all
ingestion polls include the current `attempts` count. Equivalent active ingestion,
tour, and issue-brief requests reuse an existing job instead of racing. Brief responses
add a phase so clients can distinguish `blocked_on_ingest`, `queued`, and `generating`.

`POST /repositories/ingest` returns the requester's waiting row. Polling it reports the
shared ingest's live status and attempts until the waiting row settles. The row's owner
can always poll it; another user can poll a row only while GitHub still reports that
repository as accessible to them, and only the owner can cancel it. The shared ingest
itself has no owner and cannot be cancelled through the API. Journey and issue-brief polling, listing, and
cancellation remain strictly owner-scoped.

GitHub connect requires expiring user-to-server OAuth credentials with a refresh token;
non-expiring or already-expired tokens are rejected. The token is used only during the
request for identity and installation-ownership checks and is never persisted.

### Direct browser calls

The browser calls this API directly with the Clerk session JWT. The Next.js routes that
remain are limited to the GitHub App installation and OAuth redirect flow.

**CORS** is in place: `CORSMiddleware` in `app/main.py` reads `CORS_ORIGINS`
(comma-separated, default `http://localhost:3000`; production adds the Vercel frontend
origin). Exact origins only, bearer-header auth (`allow_credentials=False`), and
`expose_headers=["Retry-After"]` so browser JavaScript can read rate-limit headers on
`429` responses. Token-derived identity is also complete: repository, GitHub, agent,
search, ingest, journey, and brief routes use only the verified JWT `sub`.

The error contract is `HTTPException` → `{"detail": "..."}`; the frontend's shared
fetch helper surfaces string `detail` values directly in the UI and falls back to the
HTTP status when the error body is missing, malformed, non-JSON, or uses another shape.

### Rate limiting

The authenticated Clerk user ID keys atomic fixed-window counters in the `rate_limits`
table. Limits apply to `POST /api/v1/agent/ask`, repository ingest/search,
`GET /api/v1/repositories/contribution-target`, journey creation, and issue-brief
preview/creation. Preview and create share one issue-brief bucket. `POST
/api/v1/repositories/follows` is charged to the ingest bucket only when it queues a new
ingest for the user; following an indexed repository, or rejoining an ingest the user
already has queued, is free. A follow over the limit is rolled back entirely.

`POST /api/v1/briefs` is charged to the issue-brief bucket, and also to the ingest
bucket when the target `repo@ref` is unindexed and the request starts a new shared
ingest. Joining a shared ingest someone else already started is free. A brief over the
ingest limit is rolled back together with its ingest, and its `429` detail reads
`Repository indexing limit reached. Try again later.` so the frontend can tell the two
limits apart. When the worker finds that a brief's code changed since indexing, the
refresh ingest it would start is charged to the brief's requester the same way. If
that bucket is empty, the worker skips the refresh and drafts the brief with stale
citations disclosed. If the counter store is unavailable there, the brief is retried
as a transient failure.

Polling, listing, and cancellation routes are not limited. Exceeded limits return
`429` with `Retry-After`. If the counter store is unavailable, protected routes fail
closed with `503`.

The limiter intentionally uses a short transaction that commits before the route
handler starts its own database work. Thus, an allowed protected request performs two
sequential pool checkouts, not two simultaneous checkouts. The exceptions are follows
and briefs that queue an ingest: they charge the limit inside their open transaction,
so they briefly hold two connections. Those write blocks run on a worker thread, never
on the event loop: an overlapping request for the same repository waits in Postgres on
the uncommitted rows, and if it waited on the loop the first request could never
resume to commit. Size
`DATABASE_POOL_SIZE` and `DATABASE_MAX_OVERFLOW` for the resulting checkout rate and
database latency. Across multiple backend processes, the maximum application
connection count is `processes × (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)`; keep
that below the Postgres connection budget.

### Account deletion

A verified Clerk `user.deleted` event calls `delete_local_account_data` in one database
transaction. The service removes the user's background jobs and artifacts, rate-limit
counters, GitHub connection metadata, and profile. It removes indexed code chunks only
when no remaining Camino connection references the same GitHub installation; database
cascades then remove the associated embeddings.

The cleanup uses set-based deletes, so a missing user and repeated webhook deliveries are
successful no-ops. Any database or unexpected failure rolls back the transaction, is
logged without returning internal details, and produces `500` so Clerk can retry.
When the last connection to an installation is removed, its repository index-state
registry rows are deleted with the chunks.

Account deletion is initiated through Clerk's authenticated UserButton security UI,
which requires the user to type `Delete account` before continuing. Clerk deletes the
identity and sends the verified `user.deleted` webhook that triggers this local cleanup;
Camino does not need a separate delete endpoint or confirmation UI for that flow. The
cleanup removes Camino's stored GitHub connection metadata, but it does not uninstall
or revoke the external GitHub App authorization.

### GitHub installation deletion

A signed GitHub `installation.deleted` webhook removes every local connection,
background job, repository index-state row, code chunk, and cascading embedding
associated with that installation. Cleanup is
set-based and transactional, so shared organization installations and webhook retries
are handled safely. Failures roll back and return `500` so GitHub can retry; invalid
signatures return `401`.

Configure the GitHub App to deliver installation events to `POST /webhooks/github`.
Requests are verified with the HMAC secret in `GH_WEBHOOK_SECRET`. The handler also
marks every shared connection inactive on `suspend` and active again on `unsuspend`.

---

## Eval Harnesses

```bash
cd Backend
uv run python -m eval.ingest_local          # clone FastAPI 0.115.6 + ingest
uv run python -m eval.run_eval --k 5        # run against golden set
uv run python -m eval.run_agent_smoke_eval  # live agent + citation smoke check
uv run python -m eval.run_structural_eval   # tour artifact validator fixtures
uv run python -m eval.run_tour_smoke_eval   # live tour generation smoke test
uv run python -m eval.run_tour_judge_eval   # LLM-as-judge: faithfulness/relevance/completeness/ordering
```

The tour judge scores generated (or `--from-fixture`) tours 1-5 per dimension against a
committed baseline (`eval/judge_baseline.json`, overall 4.44); `--strict --min-score`
gates on it and `--judge-model` decouples judge from generator.

See [eval/README.md](eval/README.md) and [eval/EXPERIMENTS.md](eval/EXPERIMENTS.md).

**Shipped defaults** (`search.py`): `top_n=60`, `rrf_k=60`, equal RRF weights,
`path_penalty=0.3`, `filter_demo_paths=True`.

---

## Tests

Use the secret-free suite for quick feedback while developing:

```bash
uv run pytest
```

This command never uses Doppler or application credentials. Most tests run, while
the real-PostgreSQL cases use uniquely named scratch databases if the sanitized local
PostgreSQL endpoint is reachable and otherwise skip. They build their schema with the
migrations, as deploys do. On a fresh
checkout, the structural check against the untracked FastAPI fixture also skips. Use
`-rs` to display skip reasons.

Before merging any backend PR, run the complete suite:

```bash
./scripts/test_all.sh
```

On first use, the script shallow-clones the pinned FastAPI `0.115.6` fixture into
the gitignored `eval/.data/fastapi` directory, so network access is required once.
If an earlier clone was interrupted, the script preserves that incomplete directory
with an `.incomplete.<timestamp>.<pid>` suffix and installs a fresh clone automatically.
It then starts an isolated PostgreSQL 16 + pgvector container on loopback, waits
for it, runs pytest with a passwordless `TEST_DATABASE_URL`, and removes the
container on exit. With `TEST_DATABASE_URL` set, an unreachable database fails the
Postgres tests instead of skipping them, and the complete run should report **zero
skipped tests**. `TEST_DATABASE_URL` must point at an empty or migrated database on
a server where its user may create databases and roles (the migration tests create
their own scratch databases next to it). Docker
must be running; set `CAMINO_PYTEST_DB_PORT` only if the default host port `55432`
is occupied. Pytest arguments pass through, for example `./scripts/test_all.sh -q`.

Never point `TEST_DATABASE_URL` at the development or eval database. The integration
fixture truncates `jobs` and `repo_index_state`. CI may provide its own dedicated
throwaway database through `TEST_DATABASE_URL`, but it must also set `DATABASE_URL`
to a parseable synthetic database name that differs from the test database. The
fixture refuses destructive tests when the original application database identity
is unknown or matches the test database.

Current focused coverage includes retrieval/search tests, agent smoke helpers,
ref-aware staged-generation ingestion and archive limits, contribution-target and live
repository-access checks, shared job enqueue/deduplication/cancellation, structural tour
validation, issue-brief generation, cooperative cancellation, journey, brief, and
repository-ingestion route tests, CORS origin/preflight/`Retry-After` exposure,
fixed-window rate-limit behavior, Clerk
JWT validation, token-derived query scoping, and rejection of legacy `userId`
paths/body fields. Migration coverage builds the schema from an empty database with
only `DATABASE_URL`, compares it with the models, and checks lock-timeout retries,
concurrent migrators, the rollback opt-in, adoption, and that startup runs as a role
that cannot change the schema.
Account-deletion tests cover full and shared-installation
cleanup, idempotent webhook replay, rollback, retryable failures, and signature rejection.
GitHub installation tests cover set-based cleanup, idempotent replay, rollback,
retryable failures, and signature rejection.
