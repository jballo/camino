# Camino — Backend

FastAPI service: GitHub repo ingest, hybrid code search, and a LangGraph agent that
answers questions grounded in retrieved chunks.

**Current focus:** retrieval quality (see [eval/EXPERIMENTS.md](eval/EXPERIMENTS.md)).
**Next up:** cross-encoder reranker, then the guided-tour generation pipeline.

---

## Stack

- **FastAPI** + SQLModel + Postgres with **pgvector**
- **tree-sitter** — Python, JavaScript, TypeScript/TSX symbol extraction
- **OpenAI** — embeddings (`text-embedding-3-small`) + chat (`gpt-4o-mini` default)
- **LangGraph** — ReAct agent with a `hybrid_search` tool
- **Clerk** — JWT auth on API routes
- **PyGithub** — GitHub App installation tokens for repo access

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

### Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `OPENAI_API_KEY` | Embeddings + agent chat |
| `AGENT_MODEL` | Chat model (default `gpt-4o-mini`) |
| `CLERK_SECRET_KEY` | Clerk backend API |
| `CLERK_WH_KEY` | Clerk webhook signing secret |
| `CLERK_JWT_KEY` | Optional — local JWT verification |
| `GH_APP_ID` / `GH_APP_CLIENT_ID` / `GH_APP_SECRET` | GitHub App credentials |
| `GH_APP_PRIVATE_KEY` | GitHub App PEM (escaped newlines OK) |
| `GH_WEBHOOK_SECRET` | GitHub webhook verification |
| `ENCRYPTION_KEY` | Fernet key for token encryption at rest |

---

## Module layout

```
app/
├── main.py              # FastAPI app, DB init, HNSW + GIN indexes
├── api/
│   ├── repositories.py  # list repos, ingest, hybrid search
│   ├── agent.py         # POST /ask — LangGraph Q&A
│   └── github.py        # GitHub App OAuth / installation
├── agent/
│   ├── graph.py         # ReAct StateGraph (agent ↔ tools loop)
│   ├── runner.py        # answer_question() entry point
│   └── tools.py         # hybrid_search tool bound per request
├── services/
│   ├── parser.py        # tree-sitter chunk extraction
│   ├── embeddings.py    # build_embedding_text + OpenAI embed
│   ├── search.py        # hybrid search (vector + FTS + RRF)
│   └── search_index.py  # tsvector population SQL
├── models/              # SQLModel tables (users, chunks, embeddings, …)
└── webhooks/            # Clerk + GitHub webhook handlers

eval/
├── golden_dataset.json  # 20 hand-labeled FastAPI questions
├── run_eval.py          # retrieval metrics harness
├── ingest_local.py      # eval ingest from local clone
└── EXPERIMENTS.md       # experiment log + next steps
```

---

## Key API routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/repositories/{userId}` | List repos for GitHub installation |
| `POST` | `/api/v1/repositories/ingest` | Clone + parse + embed a repo |
| `POST` | `/api/v1/repositories/search` | Direct hybrid search (no agent) |
| `POST` | `/api/v1/agent/ask` | Ask the codebase (ReAct agent) |

All routes require `Authorization: Bearer <clerk_session_jwt>`.

---

## Eval Harnesses

```bash
cd Backend
uv run python -m eval.ingest_local          # clone FastAPI 0.115.6 + ingest
uv run python -m eval.run_eval --k 5        # run against golden set
uv run python -m eval.run_agent_smoke_eval  # live agent + citation smoke check
uv run python -m eval.run_structural_eval   # tour artifact validator fixtures
```

See [eval/README.md](eval/README.md) and [eval/EXPERIMENTS.md](eval/EXPERIMENTS.md).

**Shipped defaults** (`search.py`): `top_n=60`, `rrf_k=60`, equal RRF weights,
`path_penalty=0.3`, `filter_demo_paths=True`.

---

## Tests

```bash
uv run pytest
```
