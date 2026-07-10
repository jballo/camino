# Camino — Frontend

Next.js web app for Camino. Clerk handles auth; API routes proxy to the FastAPI backend.

**What works:** sign-in, GitHub connection management, repo ingest/reprocess,
processed-repo status, ask-the-codebase on `/explore`, and guided-tour generation
from the home page through `/generate` and `/tours/{id}`.

**Tour flow:** select a repo, make sure it has been processed, enter a topic, and click
**Generate tour**. The app creates a journey through `/api/journeys`, polls progress on
`/generate?id=...`, then opens the completed reader at `/tours/{id}`.

Every tour page distinguishes an expired Clerk session (401/403) from a missing tour
(404) and other backend errors, so users see an actionable message instead of one
generic failure. The proxy routes preserve the backend's HTTP status for this.

---

## Run locally

```bash
cd Frontend
cp .env.example .env.local
npm install
npm run dev        # http://localhost:3000
```

The backend must be running on port 8000 (see [Backend/README.md](../Backend/README.md)).

### Environment variables

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk frontend key |
| `CLERK_SECRET_KEY` | Clerk backend key (API routes) |
| `BACKEND_URL` | FastAPI base URL (default `http://127.0.0.1:8000`) |
| `NEXT_PUBLIC_APP_URL` | Public app URL for GitHub OAuth callback (default `http://localhost:3000`) |

---

## Pages

| Route | Status | Description |
|---|---|---|
| `/` | live | Guided tour request form — select repo, enter topic, create journey |
| `/explore` | **live** | Select repo → ingest → ask questions with cited sources |
| `/sign-in` | live | Clerk sign-in |
| `/generate` | live | Poll journey status and redirect to reader on completion |
| `/tours` | live | Library of the user's generated tours |
| `/tours/{id}` | live | Guided tour reader with TOC, explanations, why callouts, and snippets |
| `/settings` | live | GitHub connection status plus install/manage-repositories entry point |

---

## API routes (Next.js → Backend proxy)

Authenticated proxy routes forward the Clerk session JWT as
`Authorization: Bearer …`. Journey creation also injects `userId` from Clerk before
calling FastAPI.

| Route | Backend |
|---|---|
| `/api/repositories` | `GET /api/v1/repositories/{userId}` |
| `/api/repositories/ingest` | `POST …/ingest` |
| `/api/repositories/processed` | `GET /api/v1/repositories/{userId}/processed` |
| `/api/repositories/search` | `POST /api/v1/repositories/search` |
| `/api/agent/ask` | `POST /api/v1/agent/ask` |
| `/api/github/install` | GitHub App install redirect |
| `/api/github/authorize` | OAuth callback |
| `/api/github/setup` | GitHub App setup/update landing redirect |
| `/api/github/status` | `GET /api/v1/github/connection/{userId}` |
| `/api/journeys` | `POST /api/v1/journeys`, `GET /api/v1/journeys?repo=` |
| `/api/journeys/{id}` | `GET /api/v1/journeys/{id}` |

Completed tour artifacts render directly from the backend `TourArtifact` shape:
`title`, `topic`, `repo_name`, and ordered `steps` with file paths, line ranges,
snippets, explanations, and optional "why" notes.
