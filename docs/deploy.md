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

1. **Launch the instance.** Ubuntu 24.04 arm64 on a `t4g.small`, 20 GB encrypted
   gp3 root volume, an Elastic IP, and the IAM role from [Backups](#backups). Security group inbound: 22 from your IP only, 80 and 443
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
9. **Set up backups:** [Backups](#backups).

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
  `compose.env`; that SHA is what a rollback returns to. Then pause the nightly
  dump and take one of your own:

  ```bash
  sudo systemctl stop camino-db-dump.timer
  camino-db-dump pre-deploy
  ```

  It waits for a nightly dump that is still running. If it fails, do not deploy:
  run `sudo systemctl start camino-db-dump.timer` and fix the dump first. Then
  `git pull`, set
  `CAMINO_IMAGE_TAG` to the new short SHA, and run the same three commands as the
  first start: `build`, `run --rm migrate`, `up -d`. Whether the deploy worked or
  not, finish with `sudo systemctl start camino-db-dump.timer`, and if the release
  changed `deploy/backup/`, update the installed copies
  ([Install on the box](#install-on-the-box-once)). If `migrate` fails, stop
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

3. **Follow one of these paths.** Never roll back to a release older than the
   first production deploy. That deploy already includes the Alembic baseline
   (revision `20261029_01`); older images have no `app.migrate` and change the
   schema at startup themselves.

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
(step 2). Putting the database itself back is a separate step,
[Restoring from a dump](#restoring-from-a-dump), and loses every write made
after the dump.

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

## Backups

`camino-db-dump` (`deploy/backup/dump-db.sh`) dumps the database every night at
03:00 UTC and before every deploy. It keeps recent dumps in
`/var/backups/camino` and copies each one to S3.

- **What a dump holds:** every table's schema, and the rows of users, GitHub
  connections, follows, rate limits, and jobs (every brief and tour lives in
  a job). It leaves out the rows of `code_chunks`, `code_chunk_embeddings`, and
  `repo_index_state`: they are the index cache, which re-ingesting rebuilds. The
  index state goes with the chunks because, restored on its own, it would mark
  repos as indexed with nothing behind them.
- **What it can lose:** a restore loses every write made after the dump. With
  one dump a night, that is up to a day, plus up to 15 minutes of timer jitter,
  plus any nights whose dump failed.
- **How it is checked:** pg_dump runs inside the `db` container over its local
  socket, so no password is involved. Before a dump is kept, every row in it is
  decoded once (`pg_restore --file=/dev/null`), and a `.sha256` file is written
  next to it. A dump without its `.sha256` is incomplete; never restore one.
- **Retention:** the newest 14 nightly and 3 pre-deploy dumps on the box. In S3,
  a dump can expire once it is 30 days old; an expired or overwritten version can
  be deleted for good 7 days after that. S3 runs these rules in the background,
  so deletion can come a little later, never earlier.
- **Privacy:** dumps are compressed, not encrypted. They hold email addresses,
  issue text, and code snippets, so they stay in a directory only the deploy
  user can read, on the encrypted root volume, and in an encrypted bucket
  reached over HTTPS.

### S3 (once, from your own machine or the console)

1. **Create a bucket** in the box's region: Block Public Access on (all four
   settings), Object Ownership "Bucket owner enforced", Versioning on, default
   encryption SSE-S3.
2. **Refuse plain HTTP** with a bucket policy:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Sid": "DenyInsecureTransport",
       "Effect": "Deny",
       "Principal": "*",
       "Action": "s3:*",
       "Resource": ["arn:aws:s3:::<bucket>", "arn:aws:s3:::<bucket>/*"],
       "Condition": {"Bool": {"aws:SecureTransport": "false"}}
     }]
   }
   ```

3. **Expire old dumps.** Save this as `lifecycle.json` and run
   `aws s3api put-bucket-lifecycle-configuration --bucket <bucket> --lifecycle-configuration file://lifecycle.json`:

   ```json
   {
     "Rules": [
       {
         "ID": "expire-dumps",
         "Status": "Enabled",
         "Filter": {"Prefix": "db/"},
         "Expiration": {"Days": 30},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
       },
       {
         "ID": "remove-expired-delete-markers",
         "Status": "Enabled",
         "Filter": {"Prefix": "db/"},
         "Expiration": {"ExpiredObjectDeleteMarker": true}
       }
     ]
   }
   ```

4. **Give the box upload-only access.** Create an IAM role for EC2 with only
   this policy and attach it to the instance:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": "s3:PutObject",
       "Resource": "arn:aws:s3:::<bucket>/db/*"
     }]
   }
   ```

   The box can add dumps but cannot list, read, or delete them. Someone who
   breaks into the box can still overwrite a dump; versioning keeps the
   overwritten version for the 7 days above, so check the bucket within a week
   of a break-in. No access key is stored on the box.
5. **Restores use your own AWS login**, which needs `s3:ListBucket` on the bucket
   and `s3:GetObject` on `db/*` (plus `s3:ListBucketVersions` and
   `s3:GetObjectVersion` to fetch an overwritten version). Never put its keys on
   the box.

### Install on the box (once)

From the checkout, as the user who runs Compose:

```bash
sudo snap install aws-cli --classic
sudo install -d -m 700 -o "$USER" -g "$USER" /var/backups/camino
sudo install -m 755 deploy/backup/dump-db.sh /usr/local/bin/camino-db-dump
[ -e /etc/camino/backup.conf ] || install -m 600 /dev/null /etc/camino/backup.conf
```

The script is installed outside the checkout, so a rollback that checks out an
older commit keeps it. Fill `/etc/camino/backup.conf` in an editor (no quotes):

```
CAMINO_BACKUP_S3_URI=s3://<bucket>/db
CAMINO_BACKUP_S3_REGION=<region>
```

Then the timer. The service file assumes the user `ubuntu` and the checkout
`/home/ubuntu/camino`; edit `User=` and `WorkingDirectory=` if yours differ:

```bash
sudo cp deploy/backup/camino-db-dump.service deploy/backup/camino-db-dump.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start camino-db-dump.service
sudo systemctl enable --now camino-db-dump.timer
```

The `start` takes one dump right away, which proves the user, paths, and S3
role work. Check that it shows up both in `/var/backups/camino` and in
`aws s3 ls s3://<bucket>/db/` (run on your machine).

When a release changes anything in `deploy/backup/`, update the installed copies.
This leaves `backup.conf` alone, but the copied service file replaces any `User=`
or `WorkingDirectory=` edits: make them again before `daemon-reload`.

```bash
sudo install -m 755 deploy/backup/dump-db.sh /usr/local/bin/camino-db-dump
sudo cp deploy/backup/camino-db-dump.service deploy/backup/camino-db-dump.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### Check it

- `systemctl list-timers camino-db-dump.timer`: when it last ran and runs next.
- `systemctl status camino-db-dump.service`: whether the last run worked;
  `journalctl -u camino-db-dump.service -n 50` shows its messages.
- `ls -lt /var/backups/camino | head`: the newest `-nightly.dump` should be about
  a day old at most (up to about 24¼ hours with the timer's random delay), with
  its `.sha256` next to it. A dump on the box does not prove it reached S3; only
  the service result does. A failed upload is not retried: S3 then holds at most
  the dump without its `.sha256`, which cannot be restored from there, and only
  the copy on the box is complete.
- `df -h /` and `du -sh /var/backups/camino`: keep a few GB free for Docker
  images, logs, and Postgres.

Nothing alerts on a failed dump yet; check once a week until alerts exist.
`Persistent=true` catches up a run missed while the box was off, but never
retries one that failed.

## Restoring from a dump

Restoring puts the whole database back to the moment of a dump and loses every
write made after it. Use it when data is lost or damaged (a bad migration, a
mistaken delete, a lost volume). A normal rollback does not need it.

Run everything from the checkout, with these two helpers:

```bash
dc() { docker compose --env-file /etc/camino/compose.env -f docker-compose.prod.yml "$@"; }
pg() { dc exec -T db psql --no-password -h /var/run/postgresql -U camino -v ON_ERROR_STOP=1 "$@"; }
```

1. **Stop the timer and wait for any dump in progress:**

   ```bash
   sudo systemctl stop camino-db-dump.timer
   flock --wait 900 /var/backups/camino/.lock true
   ```

   If `flock` waits, a dump is still finishing.

   Do not deploy or run anything else against the database until the end.
2. **Get the dump and its `.sha256`.** Local dumps are in `/var/backups/camino`.
   For one from S3, download both files on your own machine with your own AWS
   login, check them there (`sha256sum -c <name>.sha256`, or
   `shasum -a 256 -c` on macOS), and copy both into `/var/backups/camino` with
   `scp`.
3. **Check its checksum on the box** (`<name>` is the dump's file name):

   ```bash
   (cd /var/backups/camino && sha256sum -c <name>.sha256)
   ```

4. **Stop the writers:** `dc stop api worker`. Caddy answers `503` until the end.
   If the `pgdata` volume was lost, start an empty database instead, which the
   swap below sets aside, and do not run `migrate` on it:

   ```bash
   dc up -d --wait db
   ```

5. **Decode it, then restore it into a fresh database:**

   ```bash
   dc exec -T db pg_restore --file=/dev/null < /var/backups/camino/<name>
   pg -d postgres -c 'DROP DATABASE IF EXISTS camino_restore' -c 'CREATE DATABASE camino_restore'
   dc exec -T db pg_restore --no-password -h /var/run/postgresql -U camino -d camino_restore \
     --no-owner --exit-on-error --single-transaction < /var/backups/camino/<name>
   ```

6. **Fail the work that was in flight.** Jobs that were pending or running when
   the dump was taken would otherwise run against the empty index cache (a tour
   can fail for good with "no grounded steps"), and their dedupe keys would
   block the same request from being made again:

   ```bash
   pg -d camino_restore <<'SQL'
   UPDATE public.jobs
   SET status = 'failed',
       error = 'Restored from backup; request indexing, then retry this work.',
       claimed_at = NULL,
       claimed_by = NULL,
       "updatedAt" = now()
   WHERE status IN ('pending', 'running');
   SQL
   ```

   Finished briefs and tours are untouched; they keep their own snippets and do
   not need the cache.
7. **Swap the databases**, keeping the current one as `camino_old`:

   ```bash
   pg -d postgres <<'SQL'
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE datname IN ('camino', 'camino_restore') AND pid <> pg_backend_pid();
   ALTER DATABASE camino RENAME TO camino_old;
   ALTER DATABASE camino_restore RENAME TO camino;
   SQL
   ```

   If `camino_old` already exists from an earlier restore, drop it first. A
   terminated session can take a moment to exit, so a rename can fail with
   "being accessed by other users". The block stops at the first error: if the
   first rename failed, nothing changed, so wait a few seconds and run the block
   again; if only the second failed, run just that one,
   `pg -d postgres -c 'ALTER DATABASE camino_restore RENAME TO camino'`.
8. **Check the schema revision:**

   ```bash
   dc run --rm migrate python -m app.migrate current
   ```

   It prints `database:` and `release:`. They must match before you go on; the
   command never fails on its own when they differ. If the dump is older than
   the release, either run the release the dump came from, or upgrade on purpose
   with `dc run --rm migrate`. To give up instead, swap back, then go on with
   step 9:

   ```bash
   pg -d postgres <<'SQL'
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE datname IN ('camino', 'camino_old') AND pid <> pg_backend_pid();
   ALTER DATABASE camino RENAME TO camino_restore;
   ALTER DATABASE camino_old RENAME TO camino;
   SQL
   ```

9. **Start everything:** `dc up -d`, then check `dc ps` and the `/health` probe,
   and sign in and open a brief. `/health` alone proves nothing here: it never
   touches the database.
10. **Clean up** once the data looks right. Drop the copy you did not keep:
    `camino_old` after a restore, `camino_restore` after a swap back.

    ```bash
    pg -d postgres -c 'DROP DATABASE camino_old'
    sudo systemctl start camino-db-dump.timer
    ```

11. **Rebuild the index cache.** Request indexing again in Explore for the repos
    people use. These list the candidates (follows miss repos people reach
    through their GitHub App installation, so check both):

    ```bash
    pg -d camino -c "SELECT repo_name, ref, max(\"updatedAt\") AS last_indexed FROM jobs WHERE job_type = 'repository_ingest' AND status = 'complete' GROUP BY 1, 2 ORDER BY 3 DESC"
    pg -d camino -c 'SELECT repo_name, count(*) AS followers FROM user_repo_follows GROUP BY 1 ORDER BY 2 DESC'
    ```

    Until a repo is indexed again, Explore still says "Index ready" for it
    (#144), and requesting indexing still works.

Rate limits are restored as they were; their windows expire on their own.

## Rules

- The env files never leave `/etc/camino` and are never pasted into a chat or an
  agent session. Agents may run the Compose commands above, but never the Compose
  `config` subcommand without `--quiet` and never container inspection commands,
  because both print the resolved environment.
- The API port is not published; only Caddy reaches it.

## Not done yet

Tracked on the project board: the move to RDS, whose automated backups and
point-in-time recovery replace the nightly dump, and alerts (today a failed dump
shows only in the journal).
