# Camino — Frontend

Next.js web app for Camino. Clerk handles auth, and browser pages call the FastAPI
backend directly with Clerk session JWTs. Next.js routes remain only for the GitHub App
install and OAuth redirect flow.

**What works:** sign-in, account deletion through Clerk's UserButton, GitHub connection
management, repo ingest/reprocess, processed-repo status, ask-the-codebase on `/explore`,
and guided-tour generation from the home page through `/generate` and `/tours/{id}`.
Costly backend POST routes are protected by per-user rate limits.

**Tour flow:** select a repo, make sure it has been processed, enter a topic, and click
**Generate tour**. The app creates a journey through FastAPI, polls progress on
`/generate?id=...`, then opens the completed reader at `/tours/{id}`.

Every tour page distinguishes an expired Clerk session (401/403) from a missing tour
(404) and other backend errors, so users see an actionable message instead of one
generic failure.

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

The backend must be running on port 8000 (see [Backend/README.md](../Backend/README.md)).
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

After deployment, smoke-test the complete browser flow: sign in, connect GitHub, list
and ingest a repository, ask a question, generate a tour, and poll it to completion.

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
