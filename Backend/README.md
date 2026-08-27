# Camino — Backend

FastAPI service: GitHub App connection storage, repo ingest, hybrid code search,
ask-the-codebase Q&A, backend guided-tour generation, per-user API rate limiting, and
Clerk account-lifecycle and GitHub installation webhook handling.

**Retrieval loop:** paused at a tuned stack — exp1–5 shipped (hit@5 0.900), plus an
optional exp6 cross-encoder reranker (BGE blend → 0.950). See
[eval/EXPERIMENTS.md](eval/EXPERIMENTS.md).
**Phase 2 status:** the guided-tour backend is wired end-to-end with the frontend.
The Plan → Retrieve → Draft → Review graph, durable Postgres-backed `TourJob` queue,
and `/api/v1/journeys` create/poll/list routes back the frontend's direct API calls
from the `/generate` polling page, `/tours` library, and `/tours/{id}` reader UI.

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
```

API docs: http://127.0.0.1:8000/docs

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
| `RATE_LIMIT_REPOSITORY_SEARCH_REQUESTS` / `RATE_LIMIT_REPOSITORY_SEARCH_WINDOW_SECONDS` | Direct-search limit (default 60 requests / 60 seconds) |
| `RATE_LIMIT_JOURNEY_CREATE_REQUESTS` / `RATE_LIMIT_JOURNEY_CREATE_WINDOW_SECONDS` | Journey creation limit (default 5 requests / 3600 seconds) |
| `RUN_WORKER` | Start the tour worker in the API process (default `true`) |
| `WORKER_POLL_INTERVAL` | Seconds between empty-queue polls (default `1.5`) |
| `WORKER_LEASE_TIMEOUT` | Seconds before a dead worker's claim is stale (default `1800`) |
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
- Add explicit request/model deadlines and cap repository file count, total bytes, and
  generated chunks before cloning, embedding, or generating tours.
- Handle `SIGTERM` so an in-flight `generate_tour` can finish or be cancelled cleanly;
  stale `generating` rows are requeued or failed by the worker's lease recovery.

### RDS and migrations

The current lifespan hook in `app/main.py` runs `CREATE EXTENSION`,
`SQLModel.metadata.create_all()`, idempotent compatibility migrations for
`GithubConnections.githubUserId` and the tour-worker lease fields (`claimed_at`,
`claimed_by`, `attempts`), and index creation including the partial pending-job index.
The GitHub compatibility column stays nullable on an existing database because a
trustworthy numeric GitHub ID cannot be derived from legacy rows; reconnecting the
GitHub account fills it in, while all new application writes require it. This startup
mutation is acceptable for local development but must not be the production migration
mechanism.

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

`POST /api/v1/journeys` inserts a `pending` `tour_jobs` row. A polling worker
(`app/worker.py`, started from the API lifespan when `RUN_WORKER=true`, or via
`python -m app.worker`) claims the oldest pending job with `FOR UPDATE SKIP LOCKED`,
runs generation, and requeues or fails leases that expire. Multiple processes can
share the queue. In-flight generation is still lost if a worker is killed; the row
returns to `pending` (or `failed` after `WORKER_MAX_ATTEMPTS`) once
`WORKER_LEASE_TIMEOUT` elapses.

Leave `RUN_WORKER=true` for normal local development. Set it to `false` in API
processes when generation runs separately with `python -m app.worker`. Atomic claims
make either topology—and multiple worker processes—safe.

---

## Module layout

```
app/
├── main.py              # FastAPI app, local DB init/compatibility migration, indexes
├── worker.py            # Postgres-backed tour-job claim loop
├── rate_limit.py        # PostgreSQL fixed-window limiter dependencies
├── api/
│   ├── repositories.py  # list repos, ingest, hybrid search
│   ├── agent.py         # POST /ask — LangGraph Q&A
│   ├── journeys.py      # create/poll/list tour generation jobs
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
| `POST` | `/api/v1/repositories/ingest` | Clone + parse + embed a repo |
| `POST` | `/api/v1/repositories/search` | Direct hybrid search (no agent) |
| `POST` | `/api/v1/agent/ask` | Ask the codebase (ReAct agent) |
| `POST` | `/api/v1/journeys` | Queue a guided-tour job for an ingested repo |
| `GET` | `/api/v1/journeys/{id}` | Poll a journey job; returns artifact/error when available |
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
`POST /api/v1/journeys`; read-only polling and list routes are not limited. Exceeded
limits return `429` with `Retry-After`. If the counter store is unavailable, protected
routes fail closed with `503`.

The limiter intentionally uses a short transaction that commits before the route
handler starts its own database work. Thus, an allowed protected request performs two
sequential pool checkouts, not two simultaneous checkouts. Size
`DATABASE_POOL_SIZE` and `DATABASE_MAX_OVERFLOW` for the resulting checkout rate and
database latency. Across multiple backend processes, the maximum application
connection count is `processes × (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)`; keep
that below the Postgres connection budget.

### Account deletion

A verified Clerk `user.deleted` event calls `delete_local_account_data` in one database
transaction. The service removes the user's tour jobs and artifacts, rate-limit counters,
encrypted GitHub connection, and profile. It removes indexed code chunks only when no
remaining Camino connection references the same GitHub installation; database cascades
then remove the associated embeddings.

The cleanup uses set-based deletes, so a missing user and repeated webhook deliveries are
successful no-ops. Any database or unexpected failure rolls back the transaction, is
logged without returning internal details, and produces `500` so Clerk can retry.

Account deletion is initiated through Clerk's authenticated UserButton security UI,
which requires the user to type `Delete account` before continuing. Clerk deletes the
identity and sends the verified `user.deleted` webhook that triggers this local cleanup;
Camino does not need a separate delete endpoint or confirmation UI for that flow. The
cleanup removes Camino's stored encrypted GitHub connection, but it does not uninstall
or revoke the external GitHub App authorization.

### GitHub installation deletion

A signed GitHub `installation.deleted` webhook removes every local connection, tour job,
code chunk, and cascading embedding associated with that installation. Cleanup is
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
instead (it gets `tour_jobs` truncated); the fixture refuses to run if its database
name matches `DATABASE_URL`, including through a different host alias. The normal,
recommended command is still only `uv run pytest`; no manual test-database setup
is needed.

Current focused coverage includes retrieval/search tests, agent smoke helpers,
structural tour validation, tour generation helpers, journeys route tests,
CORS origin/preflight/`Retry-After` exposure, fixed-window rate-limit behavior, Clerk
JWT validation, token-derived query scoping, and rejection of legacy `userId`
paths/body fields. Startup coverage verifies that the compatibility DDL for the
`githubUserId` column and index is emitted.
Account-deletion tests cover full and shared-installation
cleanup, idempotent webhook replay, rollback, retryable failures, and signature rejection.
GitHub installation tests cover set-based cleanup, idempotent replay, rollback,
retryable failures, and signature rejection.
