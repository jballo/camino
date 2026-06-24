# Camino — Frontend

Next.js web app for Camino. Clerk handles auth; API routes proxy to the FastAPI backend.

**What works:** sign-in, GitHub App install, repo ingest, ask-the-codebase on `/explore`.

**What's stubbed:** home-page tour request (`/api/journeys`), `/tours` and `/generate` pages
(linked in nav but not built yet).

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
| `/` | stub | Tour request form — posts to stub `/api/journeys` |
| `/explore` | **live** | Select repo → ingest → ask questions with cited sources |
| `/sign-in` | live | Clerk sign-in |
| `/tours` | planned | Tour reader (Phase 2) |
| `/generate` | planned | Tour generation status (Phase 2) |
| `/settings` | planned | User / GitHub settings |

---

## API routes (Next.js → Backend proxy)

All authenticated routes forward the Clerk session JWT as `Authorization: Bearer …`.

| Route | Backend |
|---|---|
| `/api/repositories` | `GET /api/v1/repositories/{userId}` |
| `/api/repositories/ingest` | `POST …/ingest` |
| `/api/repositories/processed` | processed-repo listing |
| `/api/agent/ask` | `POST /api/v1/agent/ask` |
| `/api/github/install` | GitHub App install redirect |
| `/api/github/authorize` | OAuth callback |
| `/api/journeys` | **stub** — returns success, no tour generated |
