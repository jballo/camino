# Secrets usage plan

**Date:** 2026-09-23 · **Status:** dev-only phase. Revisit at the RDS move (§5).

## 1. The rule

Agents never hold a credential whose leak would be an incident. Everything else
is allowed. We get there by controlling **what credentials exist and where they
live**, not by filtering what agents print. Output scrubbing is not a control
we rely on.

The hard rules in [AGENTS.md](../AGENTS.md) (no reading real `.env`/key files,
no credential values in command text, no env dumps) stay in force at all times.

## 2. Inventory

| Secret | Used by | Where it lives | Blast radius if leaked (dev) |
|---|---|---|---|
| `DATABASE_URL` | backend | Doppler + `Backend/.env.example` | Local DB only: none while the DB listens on localhost |
| `OPENAI_API_KEY` | backend | Doppler | Money, bounded by the project budget cap (§3) |
| `CLERK_SECRET_KEY`, `CLERK_WH_KEY`, `CLERK_JWT_KEY` | backend (+ frontend secret key) | Doppler | Clerk **development** instance: test users only |
| `GH_APP_SECRET`, `GH_APP_PRIVATE_KEY`, `GH_WEBHOOK_SECRET` | backend | Doppler | **Acts as the dev GitHub App on every account/repo it's installed on**: bounded by its permissions + installations (§3) |
| `ENCRYPTION_KEY` | backend (Fernet for stored GitHub tokens) | Doppler | Decrypts stored GitHub user tokens in the dev DB |
| `GH_APP_ID`, `GH_APP_CLIENT_ID`, `GITHUB_APP_SLUG`, `NEXT_PUBLIC_*`, `BACKEND_URL` | backend/frontend | Doppler / frontend env | Not secret |

"Dev" doesn't automatically mean harmless. The GitHub App and OpenAI rows are
only cheap to leak once the §3 checks are done.

## 3. One-time checks (user, ~20 min)

- [x] **OpenAI:** the key belongs to a dedicated project with a monthly budget cap.
- [x] **GitHub dev App:** installed only where needed; permissions are the
      minimum the app uses (read contents/metadata/issues). No admin, no write
      unless a feature requires it.
- [ ] **Clerk:** keys come from the *development* instance.
- [x] **ENCRYPTION_KEY:** rotated in Doppler on 2026-09-24 after the prior key's
      exposure. Any GitHub connection encrypted with the former key must be
      reconnected before use.
- [ ] **Dev Postgres** is bound to localhost (compose publishes `5432:5432`,
      i.e. all interfaces; prefer `127.0.0.1:5432:5432`). Otherwise the
      committed `.env.example` URL is an open door on shared networks.

## 4. What agents may do in the dev phase

**Allowed** (Claude and Codex alike):
- Run anything under `doppler run` with the dev config: the app, workers,
  ingestion, evals, load tests.
- Any operation on the dev database, including migrations, `TRUNCATE`,
  `psql` via `"$DATABASE_URL"` (the variable reference, never the value).
- Read `.env.example` / `.env.sample` / `.env.template`.

**Not allowed** (unchanged from AGENTS.md):
- Reading real `.env`/key files, env dumps, printing a secret's value,
  putting a value in command text, verbose `docker compose config`,
  env-returning `docker inspect`, reading session transcripts.
- Running unit tests under `doppler run`. pytest's assertion output prints
  the whole `Settings` object, so tests use dummy values (§6).

**If a dev secret appears in any tool input/output:** disclose it at once
(what, where), then rotate it:

| Secret | Rotate at |
|---|---|
| OpenAI key | OpenAI dashboard → project → API keys |
| Clerk keys | Clerk dashboard (dev instance) → API keys / webhooks |
| GitHub App secret / private key / webhook secret | GitHub → Settings → Developer settings → the dev App |
| `ENCRYPTION_KEY` | Regenerate (§3), reconnect GitHub |
| `DATABASE_URL` | Change the local Postgres password (if localhost-bound, optional) |

Then update Doppler. That's the whole procedure; a dev leak is routine.

## 5. Production phase (RDS move): decide then, not now

- Prod secrets live in a separate store that no agent session touches:
  Doppler `prd` config or AWS Secrets Manager, injected into the EC2 box at
  deploy.
- Prod credentials are all distinct from dev (separate Clerk production
  instance, separate GitHub App, separate OpenAI project, new
  `ENCRYPTION_KEY`).
- Schema changes ship as migration files, rehearsed by agents on dev and
  applied by the deploy.
- If agents need to look at prod: an MCP server connected as a read-only
  Postgres role holds the credential (candidate: Varlock's credential proxy,
  or a Postgres MCP server).
- On that day: rewrite AGENTS.md § "Current environment" and this doc's §4.

## 6. Tooling changes (agent tasks)

1. **Tests without secrets** (done 2026-09-24): set dummy values for every required `Settings`
   field at the top of `Backend/tests/conftest.py`, before `app.config` is
   imported. Overwrite them, don't `setdefault`, so a Doppler-launched pytest
   still can't see real values. `uv run pytest` then works with no Doppler.
2. **Let agents read example env files** (done 2026-09-23). The project
   has no real `.env` files, only committed `.env.example`, so the `.env.*`
   read-deny rules were removed from `~/.claude/settings.json` and
   `~/.codex/config.toml`. The `.env` rules remain as a backstop. The Bash
   hook still blocks shell reads of any non-example `.env*` file.
3. **Same pre-run command check in both tools** (done 2026-09-23).
   `~/.codex/hooks.json` runs `~/.claude/hooks/block-secret-exposure.sh`
   before every Codex shell command, just like Claude's PreToolUse hook.
   Codex trusts a hook by its hash, so any edit to the script needs
   re-approval via `/hooks` in Codex.
4. **Compose:** bind the dev DB to `127.0.0.1` (§3).
5. **Doc links:** AGENTS.md § "Current environment" points here.
6. **Complete backend suite** (done 2026-09-24): `Backend/scripts/test_all.sh`
   fetches the pinned structural fixture, starts an isolated passwordless pgvector
   container on loopback, runs pytest, and removes the container. The destructive
   integration fixture fails closed when the original application database identity
   is unknown or matches `TEST_DATABASE_URL`.
