# Camino — Backend

FastAPI service: GitHub App connection storage, repo ingest, hybrid code search,
ask-the-codebase Q&A, backend guided-tour generation, per-user API rate limiting, and
Clerk account-lifecycle and GitHub installation webhook handling.

**Retrieval loop:** paused at a tuned stack — exp1–5 shipped (hit@5 0.900), plus an
optional exp6 cross-encoder reranker (BGE blend → 0.950). See
[eval/EXPERIMENTS.md](eval/EXPERIMENTS.md).
**Phase 2 status:** the guided-tour backend is wired end-to-end with the frontend.
The Plan → Retrieve → Draft → Review graph and durable shared Postgres `Job` queue
back asynchronous repository ingestion and the `/api/v1/journeys` create, poll, list,
and cancel flow used by `/generate`, `/tours`, and `/tours/{id}`.

---

## Stack

- **FastAPI** + SQLModel + Postgres with **pgvector**
- **tree-sitter** — Python, JavaScript, TypeScript/TSX symbol extraction
- **OpenAI** — embeddings (`text-embedding-3-small`) + chat (`gpt-4o-mini` default)
- **LangGraph** — ReAct Q&A agent plus structured tour generation graph
- **Clerk** — JWT auth on API routes plus signed account-lifecycle webhooks
- **PyGithub** — GitHub App installation tokens for repo access
- **PostgreSQL fixed windows** — atomic, per-Clerk-user limits for costly POST routes

---

## Run locally

```bash
# From repo root — start Postgres
docker compose up -d

cd Backend
cp .env.example .env        # fill in secrets (see below)
uv sync
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
service, and runs the same `python -m app.worker` entrypoint.

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
| `OPENAI_API_KEY` | Embeddings + agent chat |
| `AGENT_MODEL` | Chat model (default `gpt-4o-mini`) |
| `CORS_ORIGINS` | Allowed browser origins, comma-separated (default `http://localhost:3000`) |
| `CLERK_SECRET_KEY` | Clerk backend API |
| `CLERK_WH_KEY` | Clerk webhook signing secret |
| `CLERK_JWT_KEY` | Optional — local JWT verification |
| `GH_APP_ID` / `GH_APP_CLIENT_ID` / `GH_APP_SECRET` | GitHub App credentials |
| `GH_APP_PRIVATE_KEY` | GitHub App PEM (escaped newlines OK) |
| `GH_WEBHOOK_SECRET` | GitHub webhook verification |
| `ENCRYPTION_KEY` | Fernet key for token encryption at rest |
| `RATE_LIMIT_AGENT_ASK_REQUESTS` / `RATE_LIMIT_AGENT_ASK_WINDOW_SECONDS` | Q&A limit (default 20 requests / 600 seconds) |
| `RATE_LIMIT_REPOSITORY_INGEST_REQUESTS` / `RATE_LIMIT_REPOSITORY_INGEST_WINDOW_SECONDS` | Ingest limit (default 2 requests / 3600 seconds) |
| `INGEST_MAX_TARBALL_BYTES` | Maximum compressed GitHub tarball download size (default `209715200`, or 200 MiB) |
| `INGEST_MAX_EXTRACTED_BYTES` | Maximum cumulative expanded archive size (default `1073741824`, or 1 GiB) |
| `INGEST_MAX_ARCHIVE_ENTRIES` | Maximum tar archive member count (default `100000`) |
| `INGEST_WAVE_CHUNKS` | Parsed chunks embedded and persisted per ingestion wave (default `256`) |
| `INGEST_MAX_CHUNKS` | Hard per-repository chunk cap; oversized ingests fail permanently (default `25000`) |
| `RATE_LIMIT_REPOSITORY_SEARCH_REQUESTS` / `RATE_LIMIT_REPOSITORY_SEARCH_WINDOW_SECONDS` | Direct-search limit (default 60 requests / 60 seconds) |
| `RATE_LIMIT_JOURNEY_CREATE_REQUESTS` / `RATE_LIMIT_JOURNEY_CREATE_WINDOW_SECONDS` | Journey creation limit (default 5 requests / 3600 seconds) |
| `RUN_WORKER` | Start the shared job worker in the API process (default `false`; use only for an explicitly combined deployment) |
| `WORKER_POLL_INTERVAL` | Seconds between empty-queue polls and maximum heartbeat interval (default `1.5`) |
| `WORKER_LEASE_TIMEOUT` | Seconds before a dead worker's claim is stale (default `600`) |
| `WORKER_MAX_ATTEMPTS` | Claims allowed before stale recovery marks a job failed (default `3`) |

Generate `ENCRYPTION_KEY` with:

```bash
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

---

## AWS deployment target

The backend will run on **ECS Fargate**, provisioned by the TypeScript CDK app in the
planned `Infrastructure/` directory. PostgreSQL will run on **Amazon RDS** in isolated
subnets. See the "AWS deployment plan" section of the root [README](../README.md) for
stack boundaries and deployment order.

### Backend work required before Fargate

- Add a production Dockerfile using Python 3.14, install locked `uv` dependencies, run
  as a non-root user, and start Uvicorn on `0.0.0.0:$PORT`.
- Add an unauthenticated `/health` liveness endpoint that does not depend on external
  APIs. Add a readiness check that verifies required startup configuration and database
  connectivity without calling GitHub or OpenAI.
- Validate that `installationId` submitted to `POST /api/v1/github/connect` belongs to
  an installation the authenticated GitHub user may access before persisting it.
- Revoke or uninstall the external GitHub App authorization when Clerk's confirmed
  account-deletion flow triggers the existing local cleanup service.
- Add explicit request/model deadlines and cap the parsed repository file count before
  generating tours. Compressed tarballs, expanded archive bytes, archive entries, and
  generated chunks are already capped.
- Handle `SIGTERM` so in-flight jobs can finish or be cancelled cleanly; stale
  `running` rows are requeued or failed by the worker's lease recovery.

### RDS and migrations

The current lifespan hook in `app/main.py` runs `CREATE EXTENSION`,
`SQLModel.metadata.create_all()`, and the custom composite, partial, HNSW, and GIN
indexes plus `live_code_chunks`. It intentionally does not perform compatibility
migrations. `create_all()` creates missing tables but does not alter existing ones, so
a database created before the shared `jobs` table or generation-based chunk schema must
be recreated for local development or upgraded explicitly before startup.

Before connecting ECS to RDS:

1. Add Alembic and create an initial migration for all SQLModel tables, the `vector`
   extension, HNSW index, and GIN index.
2. Keep schema migration permission separate from the runtime application's normal
   database access where practical.
3. Package migrations in the backend image and execute them as a one-off ECS task before
   updating the web service.
4. Make application startup validate the schema rather than mutate it.
5. Use an RDS connection URL with TLS enabled, and size
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
- `ENCRYPTION_KEY`

Non-secret settings such as `AGENT_MODEL`, `CORS_ORIGINS`, pool sizes, and rate-limit
thresholds can be plain task-definition environment variables. Secret values must not
be embedded in the Docker image, CDK source, CloudFormation outputs, or committed
`.env` files. Production `CORS_ORIGINS` must include the Vercel frontend origin.

### Job queue

Tour generation and repository ingestion insert typed `pending` rows in the shared
`jobs` table. The standalone polling worker (`python -m app.worker`) claims the oldest
pending job with `FOR UPDATE SKIP LOCKED` and dispatches it by type. Active duplicate
requests reuse the same row through a status-scoped unique deduplication key. Known
transient upstream and database errors return the job to `pending`; each claim
increments `attempts`, and the job becomes `failed` after `WORKER_MAX_ATTEMPTS`.
Repository names are case-folded for queue, index, and search identity, so casing
variants cannot create competing jobs or generations.

Repository ingestion verifies installation access with PyGithub, then streams one
GitHub tarball snapshot up to `INGEST_MAX_TARBALL_BYTES`, safely extracts it, and
parses supported source files locally. Extraction also enforces
`INGEST_MAX_EXTRACTED_BYTES` and `INGEST_MAX_ARCHIVE_ENTRIES` before parsing. The
snapshot reflects a single commit. This blocking download/extract/parse stretch runs
with `asyncio.to_thread`; embedding calls and job orchestration remain asynchronous.

Parsing, embedding, and inserts run in bounded waves of `INGEST_WAVE_CHUNKS`. Each
ingest writes a new generation that remains invisible while its waves commit. Once
complete, one short transaction updates `repo_index_state`, marks the owning job
complete, and deletes the old rows; failed waves leave the previous complete index live. The
`INGEST_MAX_CHUNKS` cap rejects oversized repositories before further embedding. Each
wave and the final publication revalidate and lock the worker's job ownership, so a
reclaimed or deleted job cannot commit more data. Single-statement reads use the
`live_code_chunks` view; multi-statement hybrid search resolves one active generation
and binds every retrieval and hydration query to it.

Multiple processes can share the queue. If a worker dies, lease recovery returns its
row to `pending` (or marks it `failed` at the attempt limit) after
`WORKER_LEASE_TIMEOUT`. The heartbeat runs at the smaller of one-third of the lease
timeout and `WORKER_POLL_INTERVAL` (1.5 seconds with the defaults); stale recovery scans
every 60 seconds.

Pending or running jobs can be cancelled through their type-specific API endpoint.
Cancellation changes the row to `cancelled` and clears its claim. The heartbeat then
causes in-flight tour generation to cancel its LangGraph task; ingestion rechecks
ownership before each wave commit and during atomic publication, preventing a cancelled
or reclaimed job from publishing more data. Cancelling a completed or failed job returns
`409`, while cancelling an already-cancelled job is idempotent.

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
├── worker.py            # Postgres-backed shared job claim/dispatch loop
├── rate_limit.py        # PostgreSQL fixed-window limiter dependencies
├── api/
│   ├── repositories.py  # list repos, enqueue/poll/cancel ingest, hybrid search
│   ├── agent.py         # POST /ask — LangGraph Q&A
│   ├── journeys.py      # create/poll/list/cancel tour generation jobs
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
├── services/
│   ├── account_deletion.py # transactional, idempotent local account cleanup
│   ├── installation_deletion.py # installation-scoped GitHub webhook cleanup
│   ├── jobs.py          # shared enqueue, deduplication, normalization and cancellation
│   ├── repository_ingestion.py # snapshot, bounded waves and atomic generation publish
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
| `GET` | `/api/v1/repositories/processed` | List indexed repos and chunk counts for the authenticated user's installation |
| `POST` | `/api/v1/repositories/ingest` | Queue repository parsing + embedding; returns `{id, status}` |
| `GET` | `/api/v1/repositories/ingest/{id}` | Poll an ingestion job; returns result/error when available |
| `POST` | `/api/v1/repositories/ingest/{id}/cancel` | Cancel an owned pending/running ingestion job |
| `POST` | `/api/v1/repositories/search` | Direct hybrid search (no agent) |
| `POST` | `/api/v1/agent/ask` | Ask the codebase (ReAct agent) |
| `POST` | `/api/v1/journeys` | Queue a guided-tour job for an ingested repo |
| `GET` | `/api/v1/journeys/{id}` | Poll a journey job; returns artifact/error when available |
| `POST` | `/api/v1/journeys/{id}/cancel` | Cancel an owned pending/running tour job |
| `GET` | `/api/v1/journeys?repo=` | List the authenticated user's journey jobs |
| `POST` | `/webhooks/clerk` | Process signed Clerk lifecycle events, including account cleanup |
| `POST` | `/webhooks/github` | Process signed GitHub installation events and clean up deleted installations |

All `/api/v1/*` routes require `Authorization: Bearer <clerk_session_jwt>`. The backend
verifies the token and uses its `sub` claim as the sole source of user identity; API
paths and request bodies do not accept `userId`. Legacy user-ID paths return `404`, and
body models reject a legacy `userId` field with `422`.
The Clerk webhook instead requires a valid Svix signature.

POST request bodies:

- GitHub connect: `{ code, installationId }`
- Repository ingest: `{ repoName }`
- Repository search: `{ query, repoName, limit? }` (`limit` defaults to `10`, maximum
  `100`)
- Agent Q&A: `{ question, repoName }`
- Journey creation: `{ repoName, topic }`

Both background job types use `pending`, `running`, `complete`, `failed`, and
`cancelled` statuses. A completed ingestion poll includes
`result: {chunks_inserted, embeddings_created}`; failed jobs include `error`, and all
ingestion polls include the current `attempts` count. Equivalent active ingestion
requests for the same installation/repository and equivalent active tour requests for
the same user/repository/topic reuse the existing job instead of racing.

An ingestion job owner can always poll it. Another user connected to the same
installation can poll only while GitHub still reports that repository as accessible,
and only the owner can cancel it. Journey polling, listing, and cancellation remain
strictly owner-scoped.

GitHub connect requires expiring user-to-server OAuth credentials with a refresh token;
non-expiring or already-expired tokens are rejected.

### Direct browser calls

The browser calls this API directly with the Clerk session JWT. The Next.js routes that
remain are limited to the GitHub App installation and OAuth redirect flow.

**CORS** is in place: `CORSMiddleware` in `app/main.py` reads `CORS_ORIGINS`
(comma-separated, default `http://localhost:3000`; production adds the Vercel frontend
origin). Exact origins only, bearer-header auth (`allow_credentials=False`), and
`expose_headers=["Retry-After"]` so browser JavaScript can read rate-limit headers on
`429` responses. Token-derived identity is also complete: repository, GitHub, agent,
search, ingest, and journey routes use only the verified JWT `sub`.

The error contract is `HTTPException` → `{"detail": "..."}`; the frontend's shared
fetch helper surfaces string `detail` values directly in the UI and falls back to the
HTTP status when the error body is missing, malformed, non-JSON, or uses another shape.

### Rate limiting

The authenticated Clerk user ID keys atomic fixed-window counters in the `rate_limits`
table. Limits apply to `POST /api/v1/agent/ask`,
`POST /api/v1/repositories/ingest`, `POST /api/v1/repositories/search`, and
`POST /api/v1/journeys`. Polling, listing, and cancellation routes are not limited.
Exceeded limits return `429` with `Retry-After`. If the counter store is unavailable,
protected routes fail closed with `503`.

The limiter intentionally uses a short transaction that commits before the route
handler starts its own database work. Thus, an allowed protected request performs two
sequential pool checkouts, not two simultaneous checkouts. Size
`DATABASE_POOL_SIZE` and `DATABASE_MAX_OVERFLOW` for the resulting checkout rate and
database latency. Across multiple backend processes, the maximum application
connection count is `processes × (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)`; keep
that below the Postgres connection budget.

### Account deletion

A verified Clerk `user.deleted` event calls `delete_local_account_data` in one database
transaction. The service removes the user's background jobs and artifacts, rate-limit
counters, encrypted GitHub connection, and profile. It removes indexed code chunks only
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
cleanup removes Camino's stored encrypted GitHub connection, but it does not uninstall
or revoke the external GitHub App authorization.

### GitHub installation deletion

A signed GitHub `installation.deleted` webhook removes every local connection,
background job, repository index-state row, code chunk, and cascading embedding
associated with that installation. Cleanup is
set-based and transactional, so shared organization installations and webhook retries
are handled safely. Failures roll back and return `500` so GitHub can retry; invalid
signatures return `401`.

Configure the GitHub App to deliver installation events to `POST /webhooks/github`.
Requests are verified with the HMAC secret in `GH_WEBHOOK_SECRET`; this handler currently
acts only on the `deleted` action.

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

```bash
uv run pytest
```

Postgres claim/recovery tests in `tests/test_worker_claim_pg.py` need a real
database for `FOR UPDATE SKIP LOCKED`. With the docker-compose Postgres running
they are included in a plain `uv run pytest`: the fixture creates a scratch
database with a unique `camino_worker_test_*` name on the `DATABASE_URL` server
and drops it after the run. It never drops a pre-existing database. If Postgres
is down, the module skips. Set `TEST_DATABASE_URL` to target an existing database
instead (it gets `jobs` truncated); the fixture refuses to run if its database
name matches `DATABASE_URL`, including through a different host alias. The normal,
recommended command is still only `uv run pytest`; no manual test-database setup
is needed.

Current focused coverage includes retrieval/search tests, agent smoke helpers,
staged-generation ingestion and archive limits, shared job enqueue/deduplication/
cancellation, structural tour validation, cooperative tour cancellation, journey and
repository-ingestion route tests,
CORS origin/preflight/`Retry-After` exposure, fixed-window rate-limit behavior, Clerk
JWT validation, token-derived query scoping, and rejection of legacy `userId`
paths/body fields. Startup coverage verifies current table/index/view provisioning.
Account-deletion tests cover full and shared-installation
cleanup, idempotent webhook replay, rollback, retryable failures, and signature rejection.
GitHub installation tests cover set-based cleanup, idempotent replay, rollback,
retryable failures, and signature rejection.
