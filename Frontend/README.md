# Camino — Frontend

Next.js web app for Camino. Clerk handles auth; API routes proxy to the FastAPI backend.

**What works:** sign-in, GitHub App install, repo ingest, ask-the-codebase on `/explore`.

**Current gap:** the backend now has real `/api/v1/journeys` job endpoints, but the
Next.js `/api/journeys` route is still a local stub. The home-page tour request,
`/generate`, and `/tours` need to be wired before guided tours are usable in the UI.

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
| `/` | partial | Tour request form — selects/ingests repos, then posts to stub `/api/journeys` |
| `/explore` | **live** | Select repo → ingest → ask questions with cited sources |
| `/sign-in` | live | Clerk sign-in |
| `/tours` | planned | Tour reader (Phase 2) |
| `/generate` | planned | Tour generation status (Phase 2) |
| `/settings` | planned | User / GitHub settings |

---

## API routes (Next.js → Backend proxy)

Live authenticated proxy routes forward the Clerk session JWT as
`Authorization: Bearer …`; `/api/journeys` still needs that wiring.

| Route | Backend |
|---|---|
| `/api/repositories` | `GET /api/v1/repositories/{userId}` |
| `/api/repositories/ingest` | `POST …/ingest` |
| `/api/repositories/processed` | processed-repo listing |
| `/api/agent/ask` | `POST /api/v1/agent/ask` |
| `/api/github/install` | GitHub App install redirect |
| `/api/github/authorize` | OAuth callback |
| `/api/journeys` | **stub** — should proxy to backend `POST /api/v1/journeys` next |

Next frontend work: make `/api/journeys` forward the Clerk JWT to FastAPI, add
`GET /api/journeys/{id}` polling for `/generate`, then render completed artifacts in
`/tours/{id}`.
