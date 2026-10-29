# Deploying the backend to one EC2 box

The private alpha runs the whole backend on a single EC2 `t4g.small` (2 vCPU ARM,
2 GiB RAM) with Docker Compose. `docker-compose.prod.yml` runs four long-lived
containers and a one-off migration step:

| Service   | Image                                 | Role                                                  |
|-----------|---------------------------------------|-------------------------------------------------------|
| `caddy`   | `caddy:2.11.7-alpine`                 | Ports 80/443, automatic TLS, reverse proxy to the API |
| `api`     | `Backend/Dockerfile`                  | FastAPI under uvicorn on port 8000 (internal only)    |
| `worker`  | same image, `python -m app.worker`    | Claims ingest, tour, and brief jobs from Postgres     |
| `db`      | `pgvector/pgvector:0.8.7-pg16-trixie` | Postgres with pgvector; data in the `pgdata` volume   |
| `migrate` | same image, `python -m app.migrate`   | Applies schema migrations, then exits ([Migrations](#migrations)) |

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
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml build
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml run --rm migrate
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml up -d
   ```

   The build takes a few minutes. `run --rm migrate` starts the database and
   creates the schema; `up -d` starts everything else and obtains the
   certificate.

7. **Check it:**

   ```bash
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml ps
   curl -i https://api.example.com/health
   ```

   Every service should be `running` and `api` and `db` `healthy`, except
   `migrate`, which shows `exited (0)`; the probe returns `{"status":"ok"}`.

8. **Register the webhooks** at `https://api.example.com/webhooks/clerk` and
   `https://api.example.com/webhooks/github`, and set the GitHub App's callback URL.

## Day to day

All commands take the same `--env-file` and `-f` flags; define a shell alias.

- **Logs:** `docker compose ... logs -f api` (or `worker`, `caddy`, `db`,
  `migrate`). Caddy writes one JSON line per request.
- **While the API is down or restarting:** Caddy retries failed connections for
  about 5 seconds (a request the API already received is never sent again), then
  answers itself with the frontend's CORS headers so the browser
  lets the frontend read the reply: `204` to a CORS preflight, and
  `503 {"detail": "Service temporarily unavailable"}` with `Retry-After: 10` to
  everything else. A slow API is never taken out of rotation; requests just wait
  for it. Any status the API itself returns, including its own `502` for a failed
  GitHub check, passes through unchanged.
- **Deploy a new version:** first note the current `CAMINO_IMAGE_TAG` in
  `compose.env`; that SHA is what a rollback returns to. Then `git pull`, set
  `CAMINO_IMAGE_TAG` to the new short SHA, and run the same three commands as the
  first start: `build`, `run --rm migrate`, `up -d`. If `migrate` fails, stop
  there: the previous release is still running and the database is unchanged
  ([Migrations](#migrations)). `up -d` runs `migrate` once more (it finds nothing
  to do) and replaces only the `api` and `worker` containers. If
  `deploy/Caddyfile` changed, also run `up -d --no-deps --force-recreate caddy`:
  Compose does not notice edits to a mounted file, and Caddy keeps the
  configuration it started with.
- **Roll back:** see [Rolling back](#rolling-back).
- **Second worker:** add `--scale worker=2` to the `up -d` command you are running,
  keeping its other flags. Each worker is budgeted at 400 MiB.
- **Schema revision:** `run --rm migrate python -m app.migrate current` prints the
  database's revision and the one this release expects.
- **Stop everything:** `down`. Volumes (database, certificates) survive; `down -v`
  deletes them.

## Rolling back

A rollback puts the previous release's backend image back. The Caddyfile, the
Compose file, and the files in `/etc/camino` are not part of that image, so first
find out whether the release you are undoing changed them. Below,
`<previous SHA>` is the tag you noted before deploying and `<current SHA>` is the
tag running now.

1. **See what the release changed:**

   ```bash
   docker image ls camino-backend
   git diff --stat <previous SHA> <current SHA> -- deploy/Caddyfile docker-compose.prod.yml Backend/.env.example Backend/app/config.py Backend/migrations/versions
   ```

   The first command shows whether the previous image is still on the box. The
   second only lists files; read the diff of each one it lists
   (`git diff <previous SHA> <current SHA> -- <file>`) to see what changed.

2. **Put the env files back first.** `/etc/camino` is not in git, so no command
   below touches it. If the release changed `Backend/.env.example`,
   `Backend/app/config.py`, or the `${...}` variables in `docker-compose.prod.yml`,
   edit `compose.env`, `backend.env`, and `db.env` until every key the previous
   release reads is there with the value it expects: a renamed key gets its old
   name back, a changed value its old value. Keys that only the newer release reads
   can stay; the previous release ignores them.

   **If the release added a migration** (a new file under
   `Backend/migrations/versions` in step 1), the database is at a revision the
   previous release does not know, and its `api`, `worker`, and `migrate` refuse
   to start. A rollback never undoes a migration; the previous release runs
   against the newer schema instead. Print the database's revision while the
   current release is still checked out:

   ```bash
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml run --rm migrate python -m app.migrate current
   ```

   and add it to `compose.env`:

   ```
   CAMINO_ALLOW_SCHEMA_REVISION=<the database revision>
   ```

   The previous release then accepts exactly that revision, logs a warning at
   every start, and still refuses any other revision it does not know. This is
   safe because every migration stays compatible with the release before it
   ([Migrations](#migrations)). Delete the line at the next deploy.

3. **Follow one of these paths.** The release that added the Alembic baseline
   (revision `20261029_01`) is a rollback boundary: older images have no
   `app.migrate` and change the schema at startup themselves. To go back past
   it, use a checkout path below (never the tag-only one) and restore the
   database dump taken before the deploy.

   Every path first checks the schema with the previous release's own image,
   while the current release keeps serving. With the image already on the box,
   that check is:

   ```bash
   docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml up --no-build --pull never --no-deps --exit-code-from migrate migrate
   ```

   It runs only `migrate` and leaves `api` and `worker` alone. If it fails, stop
   and fix step 2 (usually the opt-in) before going on: the `up -d` below would
   otherwise stop the current containers and then fail. Do not use
   `run --rm migrate` for this check: when the image is missing, `run` builds
   the current checkout and tags it with the old SHA.

   - **Caddyfile and Compose file unchanged, previous image present:** set
     `CAMINO_IMAGE_TAG` back to `<previous SHA>`, run the check, then
     `up -d --no-build --pull never`. A release that changed only env keys takes
     this path too, once step 2 is done.

     Both flags matter: the backend services have a `build:` section, so a plain
     `up -d` with a missing image rebuilds the *current* checkout and tags it with
     the old SHA, and without `--pull never` Compose first looks for the image on
     Docker Hub. With both, a missing image fails with "No such image" instead.
     Pulling is off for every service, so the Caddy and pgvector images must also be
     on the box (they are, once the stack has run).
   - **Caddyfile or Compose file changed, previous image present:**
     `git checkout <previous SHA>`, set `CAMINO_IMAGE_TAG` back to `<previous SHA>`,
     run the check, then `up -d --no-build --pull never`. This restores that release's Compose file
     and runs its original image, without a rebuild. If the Caddyfile changed, also
     run `up -d --no-deps --force-recreate --no-build --pull never caddy` so Caddy
     loads the restored file.
   - **Previous image gone:** `git checkout <previous SHA>`, set `CAMINO_IMAGE_TAG`
     back to `<previous SHA>`, run `build`, then the check, then `up -d`, and
     recreate Caddy as above if the Caddyfile changed. The rebuilt image matches the old code but not
     necessarily the old image (the base image tag may point somewhere new).

4. **Check it** with `ps` and the `/health` probe, as in step 7 of the setup.

After a checkout, Git is on no branch and `git pull` fails. Stay there until the
next deploy, and start that deploy with `git switch <release branch>`: switching
puts the newer Compose file and Caddyfile back on disk, and any `up` run after
that would use them.

A rollback never undoes database changes: the previous release runs against the
schema the newer one migrated to, accepted through `CAMINO_ALLOW_SCHEMA_REVISION`
(step 2).

## Migrations

Only Alembic changes the schema. `migrate` runs `python -m app.migrate`
(`Backend/app/migrate.py`), which needs nothing but `DATABASE_URL`. The API and
worker never change the schema: at startup they read the database's revision in
a read-only transaction and exit with a message unless it is the revision their
release expects, or the one `CAMINO_ALLOW_SCHEMA_REVISION` names.

- **Migrate before `up -d`.** A failed `run --rm migrate` leaves the running
  release untouched. `up -d` would also stop at a failing `migrate`, but only
  after replacing the old containers, which leaves the site down until it is
  fixed.
- **A failed migration leaves nothing behind.** All pending revisions run in one
  transaction; on any error everything rolls back and the revision stays where
  it was.
- **It never waits long for a lock.** A statement waits at most 1 second to get
  a lock that a running request or job holds, and requests that queue behind
  that waiting statement wait at most as long. Then the whole upgrade rolls back
  and tries again after 2, 4, 8, and 16 seconds; after the fifth attempt it
  fails. When every attempt is blocked like this, that takes about 35 seconds.
  Until #134 is fixed, `/ask` and repository search keep a transaction open
  across their OpenAI and GitHub calls, so a deploy during heavy use of either
  may need a second try.
- **A lock it gets is held until the migration ends.** Once a statement has its
  lock (an `ALTER TABLE` locks out even reads of that table), the lock stays
  until the whole upgrade commits or rolls back, so every later statement in the
  same run extends the time requests wait. A statement may run for 2 minutes,
  waiting included; nothing bounds the run as a whole. Keep migrations that
  touch busy tables short, and split a slow one (a big backfill, an index on a
  large table) into a procedure of its own.
- **Two migrations never overlap.** A second `migrate` waits up to 2 minutes
  for the first. If the first finishes in time, the second finds nothing left to
  do; if not, it fails without changing anything.
- **Every migration stays compatible with the release before it**
  ([README.md](../README.md), "Rollback when a deploy goes wrong"): add columns
  as nullable or with a default, and drop or rename one only a release after
  the code stopped using it. That is what lets a rollback run the previous
  release against the newer schema.
- **After a Docker or host restart,** containers come back without waiting for
  each other or for `migrate`. The API and worker check the revision again, and
  `restart: always` retries them until the database is up.

## Rules

- The env files never leave `/etc/camino` and are never pasted into a chat or an
  agent session. Agents may run the Compose commands above, but never the Compose
  `config` subcommand without `--quiet` and never container inspection commands,
  because both print the resolved environment.
- The API port is not published; only Caddy reaches it.

## Not done yet

Tracked on the project board: nightly database dump, CI, and the move to RDS. Until the dump exists, the `pgdata` volume is
the only copy of users, connections, jobs, and briefs; code chunks and embeddings
are a rebuildable cache.
