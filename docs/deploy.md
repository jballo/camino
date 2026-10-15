# Deploying the backend to one EC2 box

The private alpha runs the whole backend on a single EC2 `t4g.small` (2 vCPU ARM,
2 GiB RAM) with Docker Compose. `docker-compose.prod.yml` starts four containers:

| Service  | Image                              | Role                                                     |
|----------|------------------------------------|----------------------------------------------------------|
| `caddy`  | `caddy:2.11.7-alpine`              | Ports 80/443, automatic TLS, reverse proxy to the API    |
| `api`    | `Backend/Dockerfile`               | FastAPI under uvicorn on port 8000 (internal only)       |
| `worker` | same image, `python -m app.worker` | Claims ingest, tour, and brief jobs from Postgres        |
| `db`     | `pgvector/pgvector:0.8.7-pg16`     | Postgres with pgvector; data in the `pgdata` volume      |

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
   never committed:

   ```bash
   sudo mkdir -p /etc/camino && sudo chmod 700 /etc/camino
   sudo touch /etc/camino/{compose.env,backend.env,db.env}
   sudo chmod 600 /etc/camino/*.env
   ```

   Fill the three files in an editor, not through a shell command (a command line is
   recorded in history):

   - `compose.env`: `CAMINO_DOMAIN=api.example.com`, `CAMINO_CONFIG_DIR=/etc/camino`,
     and `CAMINO_IMAGE_TAG=<git short SHA>`.
   - `backend.env`: every variable from `Backend/.env.example` that has no default,
     plus `CORS_ORIGINS=https://<the Vercel origin>` and any limits you want to change.
     `DATABASE_URL` is a `postgresql://` URL with user `camino`, the password you
     chose, host `db` (the Compose service name), port `5432`, and database `camino`.
   - `db.env`: `POSTGRES_PASSWORD=<the same password>`.

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
- **Deploy a new version:** `git pull`, set `CAMINO_IMAGE_TAG` in `compose.env` to the
  new short SHA, then `up -d --build`. Compose rebuilds the image and replaces only
  the `api` and `worker` containers.
- **Roll back:** set `CAMINO_IMAGE_TAG` back to the previous SHA and run `up -d`
  without `--build`; the old image is still on the box.
- **Second worker:** append `--scale worker=2` to `up -d`. Each worker is budgeted at
  400 MiB.
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
