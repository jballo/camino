# Camino — Frontend

Next.js web app for Camino. Clerk handles auth, and browser pages call the FastAPI
backend directly with Clerk session JWTs. Next.js routes remain only for the GitHub App
install and OAuth redirect flow.

**What works:** sign-in, account deletion through Clerk's UserButton, GitHub connection
management, queued repo ingest/reprocess with progress and cancellation,
processed-repo status, ask-the-codebase on `/explore`, and guided-tour generation from
the home page through `/generate` and `/tours/{id}`. `/briefs` also previews GitHub
issue contribution signals and generates grounded implementation briefs. Costly API
operations are protected by per-user rate limits.

**Tour flow:** select a repo, make sure it has been processed, enter a topic, and click
**Generate tour**. The app creates a journey through FastAPI, polls progress on
`/generate?id=...`, then opens the completed reader at `/tours/{id}`. Active jobs can
be stopped from the progress page. Polling pauses after ten minutes with options to
start another ten-minute polling window or leave; the server job continues unless the
user explicitly selects **Stop generating**.

Tour pages distinguish an expired Clerk session (401/403), a missing tour (404),
cancelled and failed jobs, polling timeouts, and other backend errors, so users see an
actionable message instead of one generic failure. The tours library also retains
cancelled jobs with a distinct status badge.

**Issue brief flow:** paste a full GitHub issue URL on `/briefs`. The preview shows the
issue state, labels, assignment/discussion/open-PR warnings, resolved upstream, target
branch evidence, and measurable fork drift. The branch can be overridden before
generation. The reader polls a durable brief job, shows when it is waiting on a required
repository refresh, supports cancellation, and renders the grounded summary, setup
recipe, code-reading steps, test guidance, checklist, freshness, and confidence notes.

**Repository processing:** both the home-page repository dialog and `/explore` enqueue
an ingestion job, poll it every two seconds, and show its queue/running state. Polling
times out after ten minutes without cancelling the backend job. **Stop** sends a
server-side cancellation request before stopping browser polling. Processing and reads
are ref-aware: the UI discovers a contribution target when needed and carries the
selected indexed ref into Explore, tour, and brief requests.

**Account deletion:** open Clerk's UserButton, select **Security**, and choose
**Delete account**. Clerk requires the user to type `Delete account`, deletes the Clerk
identity, and sends the backend a verified `user.deleted` webhook that removes local
Camino data. This flow does not currently uninstall or revoke the external GitHub App
authorization.

Removing the GitHub App from a GitHub account or organization is separate from Clerk
account deletion. Its `installation.deleted` webhook removes Camino's connections,
indexed repositories, tours, and embeddings for that installation.

---

## Run locally

```bash
cd Frontend
cp .env.example .env.local
npm install
npm run dev        # http://localhost:3000
```

The backend must be running on port 8000, and a shared job worker must be running for
repository processing, tour generation, and issue-brief generation (see
[Backend/README.md](../Backend/README.md)). Start it with
`uv run python -m app.worker` from `Backend/`, or use
`docker compose --profile worker up -d worker` from the repository root. Without a
worker, jobs remain queued and the UI eventually reports its ten-minute polling timeout.

Open the frontend at `http://localhost:3000` to match the backend's default
`CORS_ORIGINS`; `http://127.0.0.1:3000` is a different origin and must be added
explicitly.

For the local GitHub App flow, configure:

- Callback URL: `http://localhost:3000/api/github/authorize`
- Setup URL: `http://localhost:3000/api/github/setup`, with **Redirect on update**
  enabled

The install route currently redirects to the `camino-onboarder` GitHub App. Use that App
for local development or change `src/app/api/github/install/route.ts` until
`GITHUB_APP_SLUG` is implemented.

### Environment variables

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk frontend key |
| `CLERK_SECRET_KEY` | Clerk backend key for the remaining GitHub App routes |
| `BACKEND_URL` | Server-side FastAPI base URL used by the GitHub OAuth callback |
| `NEXT_PUBLIC_BACKEND_URL` | Browser-visible FastAPI base URL (default `http://127.0.0.1:8000`) |
| `NEXT_PUBLIC_APP_URL` | Public app URL for GitHub OAuth callback (default `http://localhost:3000`) |

---

## Production configuration

The frontend is not part of the initial RDS/ECS CDK stacks, but it must be updated when
the Fargate backend is deployed:

- Set both `BACKEND_URL` and `NEXT_PUBLIC_BACKEND_URL` to the backend's HTTPS
  ALB/custom-domain origin. The public value is intentionally browser-visible.
- Set `NEXT_PUBLIC_APP_URL` to the frontend's canonical HTTPS origin.
- Add the frontend's exact origin to the backend's `CORS_ORIGINS`.
- Configure the production frontend URL in Clerk's allowed redirect/origin settings.
- Configure GitHub App setup/callback URLs to use the production frontend routes and
  webhook URLs to use the production backend.
- Replace the hardcoded `camino-onboarder` installation URL in
  `src/app/api/github/install/route.ts` with a required server-side
  `GITHUB_APP_SLUG` environment variable.
- Make production builds fail when required URLs or credentials are absent instead of
  falling back to localhost.

After deploying both the API and worker, smoke-test the complete browser flow: sign in,
connect GitHub, list and ingest a repository, ask a question, generate a tour, generate
an issue brief, and poll both jobs to completion.

---

## Pages

| Route | Status | Description |
|---|---|---|
| `/` | live | Guided tour request form plus queued repository processing dialog |
| `/explore` | **live** | Select repo → queue/poll/cancel ingest → ask questions with cited sources |
| `/sign-in` | live | Clerk sign-in |
| `/generate` | live | Poll, time out, resume, or cancel generation; redirect on completion |
| `/tours` | live | Library with queued, generating, ready, failed, and cancelled statuses |
| `/tours/{id}` | live | Guided tour reader with TOC, explanations, why callouts, and snippets |
| `/briefs` | live | Preview a GitHub issue, verify/override its target branch, and list recent briefs |
| `/briefs/{id}` | live | Poll/cancel generation and read the grounded contribution brief |
| `/settings` | live | GitHub connection status plus install/manage-repositories entry point |

---

## Visual system

The interface uses the dark Glyph design across every route: an orange accent on a
near-black token palette, Doto display type, Space Grotesk body type, JetBrains Mono for
technical labels, console-style cards, ledger rows, and compact uppercase navigation.
The shared tokens and component classes live in `src/app/globals.css`; font loading and
Clerk appearance variables live in `src/app/layout.tsx`. Focus-visible outlines,
disabled cursors, and reduced-motion overrides are defined globally.

---

## Backend API access

`src/lib/api.ts` contains the small shared `backendFetch<T>` helper and `ApiError`.
Pages obtain a current token with Clerk's `useAuth().getToken()`, pass it explicitly to
the helper, and call `/api/v1/*` on `NEXT_PUBLIC_BACKEND_URL`. The helper attaches
`Authorization: Bearer …`, serializes JSON bodies, and throws an `ApiError` containing
the backend status and string FastAPI `detail` message when a response fails. Missing,
malformed, non-JSON, and non-string error bodies fall back to
`Request failed (<status>)`. The helper does not automatically retry requests.

Rate-limited requests surface the backend's `detail` message through `ApiError`. Although
FastAPI exposes `Retry-After`, the UI does not yet display a countdown.

The browser never sends a `userId`. FastAPI verifies the JWT and derives identity from
its `sub` claim.

The only remaining Next.js API routes are:

- `/api/github/install`, which creates the CSRF state cookie and redirects to GitHub.
- `/api/github/authorize`, which validates the state, sends the OAuth code to FastAPI,
  and redirects back to settings.
- `/api/github/setup`, which handles GitHub App installation updates.

`src/lib/repository-ingestion.ts` owns the asynchronous ingestion client:
`POST /api/v1/repositories/ingest` enqueues work, `GET .../ingest/{id}` polls status,
and `POST .../ingest/{id}/cancel` stops an owned active job. Its poller refreshes the
Clerk token on every request, reports `pending`, `running`, `complete`, `failed`, and
`cancelled`, and treats client aborts separately from ten-minute timeouts. The
`/generate` page follows the same two-second/ten-minute polling cadence for journeys
and cancels through `POST /api/v1/journeys/{id}/cancel`.

`src/lib/contribution-target.ts` resolves the target branch used when an ingest omits an
explicit ref. `src/lib/briefs.ts` previews issue metadata and branch/fork signals, then
creates, lists, polls, and cancels issue-brief jobs through `/api/v1/briefs/*`.

Completed tour artifacts render directly from the backend `TourArtifact` shape:
`title`, `topic`, `repo_name`, and ordered `steps` with file paths, line ranges,
snippets, explanations, and optional "why" notes.

---

## Tests

```bash
npm test             # run Vitest once
npm run test:watch   # watch mode
npm run lint
```

`src/lib/api.test.ts` covers successful JSON responses, bearer-token requests, JSON
POST bodies, FastAPI `detail` errors, unexpected error shapes, and non-JSON responses.
`src/lib/repository-ingestion.test.ts` covers enqueue/cancel requests, status updates,
terminal states, token refresh, polling timeouts, and abort behavior.
`src/lib/contribution-target.test.ts` covers target discovery responses, and
`src/lib/briefs.test.ts` covers authenticated preview, create, and read requests.
