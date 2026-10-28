# Deploying the backend to one EC2 box

The private alpha runs the whole backend on a single EC2 `t4g.small` (2 vCPU ARM,
2 GiB RAM) with Docker Compose. `docker-compose.prod.yml` starts four containers:

| Service  | Image                                 | Role                                                  |
|----------|---------------------------------------|-------------------------------------------------------|
| `caddy`  | `caddy:2.11.7-alpine`                 | Ports 80/443, automatic TLS, reverse proxy to the API |
| `api`    | `Backend/Dockerfile`                  | FastAPI under uvicorn on port 8000 (internal only)    |
| `worker` | same image, `python -m app.worker`    | Claims ingest, tour, and brief jobs from Postgres     |
| `db`     | `pgvector/pgvector:0.8.7-pg16-trixie` | Postgres with pgvector; data in the `pgdata` volume   |

The frontend stays on Vercel and calls the API by its hostname, so the backend
needs a real domain. Nothing here is automated yet: the steps below are run by a
person on the box.

## One-time setup

1. **Launch the instance.** Ubuntu 24.04 arm64 on a `t4g.small`, 20 GB gp3 root
   volume, an Elastic IP. Security group inbound: 22 from your IP only, 80 and 443
   from anywhere. Outbound open (GitHub, OpenAI, Clerk, Let's Encrypt).
2. **Point DNS at it.** An `A` record for the backend hostname (for example
   `api.example.com`) to the Elastic IP. Caddy cannot obtain a certificate until the
   name resolves to the box.
3. **Install Docker** with the Compose plugin, following Docker's Ubuntu instructions,
   and add your user to the `docker` group.
4. **Clone the repository** on the box and check out the release branch or tag.
5. **Create the configuration directory.** It lives outside the repository and is
   never committed. It belongs to the user who runs Compose, because Compose reads
   these files itself, as that user. Nobody else can open it:

   ```bash
   sudo install -d -m 700 -o "$USER" -g "$USER" /etc/camino
   for f in compose.env backend.env db.env; do install -m 600 /dev/null "/etc/camino/$f"; done
   ```

   Owning the files yourself gives nothing away compared with root ownership: a
   member of the `docker` group can already read any file on the box through a
   container.

   Fill the three files in an editor, without `sudo`, not through a shell command
   (a command line is recorded in history):

   - `compose.env`: `CAMINO_DOMAIN=api.example.com`,
     `CAMINO_CORS_ORIGIN=https://<the Vercel origin>` (the same origin as
     `CORS_ORIGINS` below), `CAMINO_CONFIG_DIR=/etc/camino`, and
     `CAMINO_IMAGE_TAG=<git short SHA>`.
   - `backend.env`: every variable from `Backend/.env.example` that has no default,
     plus `CORS_ORIGINS=https://<the Vercel origin>` and any limits you want to change.
     `DATABASE_URL` is a `postgresql://` URL with user `camino`, the password you
     chose, host `db` (the Compose service name), port `5432`, and database `camino`.
   - `db.env`: `POSTGRES_PASSWORD=<the same password>`.

   Compose parses these files with its own rules. Three of them matter here; every
   value in the examples below is fake.

   **Use a hex database password.** Generate it on the box:

   ```bash
   openssl rand -hex 32
   ```

   It prints 64 characters of `0-9` and `a-f`, none of which need escaping in a URL
   or in Compose. The same password goes into both files:

   ```
   # db.env
   POSTGRES_PASSWORD=5e1f0c7a9b2d4e6f8a1c3b5d7e9f0a2b4c6d8e0f1a3b5c7d9e1f3a5b7c9d1e3f

   # backend.env
   DATABASE_URL=postgresql://camino:5e1f0c7a9b2d4e6f8a1c3b5d7e9f0a2b4c6d8e0f1a3b5c7d9e1f3a5b7c9d1e3f@db:5432/camino
   ```

   A password such as `p@ss/word` would break the URL: `@` and `/` are part of URL
   syntax, so the app would try to reach the wrong host.

   **Protect any `$` in a value.** Compose reads `$` in an unquoted or double-quoted
   value as a variable reference. It drops the `$` and the rest of the word, starts
   anyway, and only prints a "variable is not set" warning. This mostly matters for
   third-party keys you do not choose yourself:

   ```
   # Wrong: the app receives "sk_ab"
   SOME_KEY=sk_ab$cd123
   SOME_KEY="sk_ab$cd123"

   # Right: the app receives "sk_ab$cd123"
   SOME_KEY='sk_ab$cd123'
   SOME_KEY=sk_ab$$cd123
   ```

   **Wrap every PEM key in double quotes.** There are two: the GitHub App private key
   `GH_APP_PRIVATE_KEY` and the Clerk public key `CLERK_JWT_KEY`. Either form below
   works for both and reaches the app with real line breaks (a real key has many more
   lines). Pasted as is:

   ```
   GH_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
   MIIEowIBAAKCAQEAfakefakefakefakefakefakefakefakefakefake
   fakefakefakefakefakefakefakefakefakefakefakefakefake==
   -----END RSA PRIVATE KEY-----"
   ```

   Or on one line, with `\n` at each line break:

   ```
   GH_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAfakefake...\nfakefake...==\n-----END RSA PRIVATE KEY-----"
   ```

   Without the quotes, the two forms fail differently:

   - Pasted as is, Compose refuses to start and reports a parse error on one of the
     key's lines (the wording depends on the key).
   - On one line, Compose starts, but the app receives a literal `\n` instead of
     line breaks. With a broken `CLERK_JWT_KEY`, every request that needs a
     signed-in user returns `401`; with a broken `GH_APP_PRIVATE_KEY`, every GitHub
     App call fails.

6. **Start the stack:**

   ```bash
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml up -d --build
   ```

   The first start builds the image (a few minutes), creates the schema, and
   obtains the certificate.

7. **Check it:**

   ```bash
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml ps
   curl -i https://api.example.com/health
   ```

   Every service should be `running` and `api` and `db` `healthy`; the probe returns
   `{"status":"ok"}`.

8. **Register the webhooks** at `https://api.example.com/webhooks/clerk` and
   `https://api.example.com/webhooks/github`, and set the GitHub App's callback URL.

## Day to day

All commands take the same `--env-file` and `-f` flags; define a shell alias.

- **Logs:** `docker compose ... logs -f api` (or `worker`, `caddy`, `db`). Caddy
  writes one JSON line per request.
- **While the API is down or restarting:** Caddy retries failed connections for
  about 5 seconds (a request the API already received is never sent again), then
  answers itself with the frontend's CORS headers so the browser
  lets the frontend read the reply: `204` to a CORS preflight, and
  `503 {"detail": "Service temporarily unavailable"}` with `Retry-After: 10` to
  everything else. A slow API is never taken out of rotation; requests just wait
  for it. Any status the API itself returns, including its own `502` for a failed
  GitHub check, passes through unchanged.
- **Deploy a new version:** `git pull`, set `CAMINO_IMAGE_TAG` in `compose.env` to the
  new short SHA, then `up -d --build`. Compose rebuilds the image and replaces only
  the `api` and `worker` containers. If `deploy/Caddyfile` changed, also run
  `up -d --no-deps --force-recreate caddy`: Compose does not notice edits to a
  mounted file, and Caddy keeps the configuration it started with.
- **Roll back:** check that `docker image ls camino-backend` lists the previous
  SHA, set `CAMINO_IMAGE_TAG` back to it, then `up -d --no-build --pull never`.
  Both flags matter: the backend services have a `build:` section, so a plain
  `up -d` with a missing image rebuilds the *current* checkout and tags it with the
  old SHA, and without `--pull never` Compose first looks for the image on Docker
  Hub. With both, a missing image fails with "No such image" instead. Pulling is
  off for every service, so the Caddy and pgvector images must also be on the box
  (they are, once the stack has run). This swaps only the backend image;
  `deploy/Caddyfile` and the Compose file still come from the current checkout.
- **Roll back when the old image is gone:** keep `CAMINO_IMAGE_TAG` at the previous
  SHA, `git checkout <previous SHA>`, then `up -d --build`. This restores that
  version's Compose file and Caddyfile on disk; if the Caddyfile differs, run
  `up -d --no-deps --force-recreate caddy` to load it. The rebuilt image matches
  the old code but not necessarily the old image (the base image tag may point
  somewhere new). Checking out a SHA leaves Git on no branch, so `git pull` fails
  until you `git switch <release branch>` before the next deploy.
- **Second worker:** add `--scale worker=2` to the `up -d` command you are running,
  keeping its other flags. Each worker is budgeted at 400 MiB.
- **Stop everything:** `down`. Volumes (database, certificates) survive; `down -v`
  deletes them.

## Rules

- The env files never leave `/etc/camino` and are never pasted into a chat or an
  agent session. Agents may run the Compose commands above, but never the Compose
  `config` subcommand without `--quiet` and never container inspection commands,
  because both print the resolved environment.
- The API port is not published; only Caddy reaches it.

## Not done yet

Tracked on the project board: nightly database dump, Alembic baseline instead of
startup DDL, CI, and the move to RDS. Until the dump exists, the `pgdata` volume is
the only copy of users, connections, jobs, and briefs; code chunks and embeddings
are a rebuildable cache.
