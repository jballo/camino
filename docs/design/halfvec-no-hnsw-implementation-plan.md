# Implementation plan: make `halfvec(1536)` with no HNSW the default

**Date:** 2026-09-24 · **Base branch:** `dev` · **Work branch:** `feat/halfvec-default`

**Status:** implementation complete on the work branch. Defaults, schema validation,
`PLAIN` storage, secret-free tests, the disposable PostgreSQL suite, and user-facing
documentation are in place. The complete suite passes with zero skips. PR and merge
state are tracked outside this implementation plan.

## Context (decided — do not re-litigate)

- `docs/storage-capacity-plan.md` § Phase 1 and `Backend/eval/EXPERIMENTS.md`
  (exp7 V2, exp8 A/B/C) settled on **`halfvec(1536)` with no ANN index**
  (exact scan).
  - Quality equals the fp32 baseline: hit@5 0.900 / recall@5 0.858 /
    MRR 0.766.
  - The largest ref measured 16 ms p95 at 11.7k chunks, against a 250 ms gate.
  - The 768 and 512 dimension options are rejected.
- Before this branch, the code supported the selected variant behind two settings.
  This change makes it the **default**:
  - `Backend/app/config.py`: the former defaults were `vector_type = "vector"`,
    `vector_index = "hnsw"`; they are now `halfvec` and `none`.
  - `Backend/app/models/code.py`: `EMBEDDING_COLUMN_TYPE` is chosen from
    `settings.vector_type` at import time.
  - `Backend/app/main.py`: `_embedding_index_ddl()` returns `None` when
    `vector_index == "none"`. Lifespan never migrates or drops existing
    columns/indexes (intentional).
  - `Backend/app/services/search.py::_vector_search_sql` casts with
    `CAST(:embedding AS {settings.vector_type}(1536))`.
- **Neon is gone** (2026-09-23). Dev runs on local Postgres; production will be
  a fresh RDS instance later. So this is a **defaults change, not a data
  migration**.

### Local databases (checked 2026-09-24)

| Container | Port | Embedding column | Rows | Action |
|---|---|---|---:|---|
| `camino-db-1` (dev DB, Doppler's `DATABASE_URL`) | 5432 | none (DB empty) | 0 | none: the app creates halfvec on first boot |
| `camino-testdb-1` (eval testdb) | 5433 | `halfvec(1536)`, PLAIN, no HNSW | 51,059 | none: already final shape |
| `camino-exp7-testdb` | none | none | 0 | none |

No fp32 database exists, so no migration SQL runs as part of this plan.

## Rules for this session

- Follow `AGENTS.md` and `docs/secrets-plan.md`. Every Doppler value is dev.
  You may run `doppler run -- …` for the app, ingestion and evals, and operate
  on the local DBs.
- **Unit tests never run under `doppler run`.** Assertion output can print the
  whole `Settings` object. Step 1 makes plain `uv run pytest` work.
- Inspect local DBs with `docker exec <container> psql -U <user> -d <db> …`.
  No URL goes in the command. Put `psql "$DATABASE_URL"` under Doppler, never a
  literal URL; the Bash hook blocks `user:password@` URLs anyway.
- Only catalog, size and count queries. Don't select columns from user or
  GitHub-connection tables.
- Commit and open the PR per the repo's `github` skill (no AI attribution).
  PR base: `dev`.

## Step 1 — tests run without secrets (prerequisite)

At the very top of `Backend/tests/conftest.py`, **before** `from app.config
import settings`, **overwrite** (not `setdefault`) every required `Settings`
field with a fake value. That way a Doppler-launched pytest still can't see
real ones:

- `DATABASE_URL`: a dummy, password-less local URL. Nothing connects to it; the
  engine is lazy.
- `CLERK_WH_KEY`, `CLERK_SECRET_KEY`, `GH_APP_CLIENT_ID`, `GH_APP_SECRET`,
  `GH_APP_PRIVATE_KEY`, `GH_WEBHOOK_SECRET`, `OPENAI_API_KEY`:
  `"SYNTHETIC_TEST_VALUE"`.
- `GH_APP_ID`: `"1"`.
- `ENCRYPTION_KEY`:
  `base64.urlsafe_b64encode(b"SYNTHETIC_TEST_VALUE_32_BYTES_!!").decode()`, a
  valid Fernet key built from obviously fake bytes.
- `os.environ.pop("CLERK_JWT_KEY", None)`, so the optional key is unset
  unless a test sets it.

Outcome: `cd Backend && uv run pytest` passes with **no Doppler**. The complete
`./scripts/test_all.sh` path provisions an isolated pgvector database and the pinned
FastAPI fixture so the PostgreSQL and structural cases run with zero skips.

## Step 2 — flip the defaults

1. `Backend/app/config.py`: set `vector_type = "halfvec"` and
   `vector_index = "none"`. Keep both settings so evals can still override
   them.
2. `Backend/.env.example`: set `VECTOR_TYPE=halfvec` and `VECTOR_INDEX=none`
   (currently `vector` and `hnsw`).
3. **Doppler check:** `doppler secrets --only-names`. If `VECTOR_TYPE` or
   `VECTOR_INDEX` exist, they override the new defaults under `doppler run`.
   These values aren't secret, so reading them with
   `doppler secrets get VECTOR_TYPE VECTOR_INDEX --plain` is fine. Ask the user
   to delete them or set them to `halfvec`/`none`.

Outcome: neither override name was present in the dev Doppler config, so the new
application defaults apply there without a configuration change.

No compose change is needed. Neither compose file passes `VECTOR_*` through,
so containers pick up the new defaults automatically.

## Step 3 — schema guard (fail fast on mismatch)

Today a config/column mismatch boots fine, and then every search fails with
`operator does not exist: vector <=> halfvec`. Add a small function, e.g.
`Backend/app/db_schema.py::verify_embedding_schema(conn)`:

- Read the column type:
  `SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = to_regclass('code_chunk_embeddings') AND attname = 'embedding' AND NOT attisdropped`.
  It must equal `f"{settings.vector_type}({EMBED_DIMENSIONS})"`. Otherwise
  raise `RuntimeError` naming the actual type, the expected type, and the
  README migration snippet (Step 5). **Never auto-migrate.**
- If `settings.vector_index == "none"` and `to_regclass('ix_embeddings_hnsw')`
  is not null, log a WARNING that the index is unused storage. Don't drop it.
- Also in lifespan's custom DDL: `ALTER TABLE code_chunk_embeddings ALTER
  COLUMN embedding SET STORAGE PLAIN`. It's idempotent and only changes table
  settings, not existing rows. `create_all` can't express it, and pgvector's
  types default to `external` storage, which would TOAST every ~3 KB halfvec
  row. The eval testdb already shows `p` because exp7 set it by hand.
- Call it in `main.py` lifespan after `create_all` and the custom DDL, so the
  table exists. Also call it at worker startup (`app/worker.py`,
  `main()` → `_run_standalone()`, ~line 922), which runs no schema setup
  today. A worker with the wrong config must not write embeddings.

## Step 4 — tests

- New defaults: `Settings` field defaults are `halfvec`/`none`.
- Guard: passes on a match; raises on `vector(1536)` vs halfvec config; warns
  but doesn't raise when HNSW exists with `vector_index="none"`. Use
  fake connection/result objects in the existing monkeypatch style.
- **Existing lifespan tests** (`tests/test_main.py`,
  `test_lifespan_provisions_schema_extras` and similar) use a `MagicMock`
  connection. The new guard query will get a MagicMock back and raise. Stub
  the guard in those tests, or have the mock return the expected type string.
- Existing tests that monkeypatch `vector`/`hnsw` explicitly should keep
  passing unchanged.
- Run `uv run pytest` (no Doppler) until green, then run `./scripts/test_all.sh` for
  the complete zero-skip suite.

## Step 5 — docs

- `docs/storage-capacity-plan.md`:
  - Status line → "phase 1 implemented: halfvec(1536), no ANN index, is the default".
  - Replace the "Implementation notes" paragraph ("defaults remain `vector` +
    HNSW until the separate production-cutover change…") with the new reality:
    defaults are final, fresh DBs boot straight into it, no production
    migration is needed (Neon dropped 2026-09-23; RDS starts fresh).
  - Phase 2: "1 GB instance RAM for hot HNSW index residency" → heap residency
    for exact scans (~3.2 KB/chunk). Drop the "(halfvec + 768)" capacity figure
    (768 rejected). "apply the phase-1 schema" → the app creates the schema on
    first boot.
  - Add a dated note that the Neon-specific problem and cutover framing is
    historical.
- `Backend/README.md` § "RDS and migrations": remove "HNSW index" from the
  lifespan and Alembic lists. Add a short **"Local DB created before
  2026-09"** note with the one-off SQL for an old fp32 volume (or: recreate the
  volume):
  ```sql
  DROP INDEX IF EXISTS ix_embeddings_hnsw;
  ALTER TABLE code_chunk_embeddings
    ALTER COLUMN embedding TYPE halfvec(1536) USING embedding::halfvec(1536),
    ALTER COLUMN embedding SET STORAGE PLAIN;
  ANALYZE code_chunk_embeddings;
  ```
- `README.md` ~line 417 checklist ("HNSW embedding col") → "halfvec embedding
  col (exact scan)".
- `docker-compose.testdb.yml` comment (~line 18) "HNSW/GIN indexes" → "GIN
  index".
- `Backend/eval/README.md`: exp7 commands pass `VECTOR_*` explicitly, so leave
  them. Add one line saying the defaults now equal exp7 V2.

## Step 6 — end-to-end verification on the fresh dev DB (agent runs)

This proves the default path works from an empty database, not just a migrated
one:

1. **Create the schema by booting the API once:**
   `cd Backend && doppler run -- env RUN_WORKER=false uv run fastapi run app/main.py --port 8010`
   (background). Wait for startup and confirm the log shows no guard error or
   HNSW warning, then stop it.
2. **Check the schema:**
   `docker exec camino-db-1 psql -U agent -d onboarding_agent -Atc "SELECT format_type(atttypid, atttypmod), attstorage::text FROM pg_attribute WHERE attrelid='code_chunk_embeddings'::regclass AND attname='embedding'; SELECT to_regclass('ix_embeddings_hnsw');"`
   Expect `halfvec(1536)`, `p` (from the Step 3 DDL), and an empty/NULL
   index.
3. **Ingest the eval fixture into the dev DB:**
   `doppler run -- uv run python -m eval.ingest_local`. This clones FastAPI
   0.115.6 if missing, about 5k chunks, about $0.25 of embeddings.
4. **Reproduce exp7 V2 on the default path:**
   `doppler run -- uv run python -m eval.run_eval --label halfvec_default_devdb --out eval/runs/halfvec_default_devdb.json`.
   Expect **hit@5 0.900 / recall@5 0.858 / MRR 0.766**, the same q03/q17
   misses. A different result means the default path isn't V2; investigate
   before the PR.
5. **Sizes, for the PR description:**
   `pg_total_relation_size('code_chunk_embeddings')` and the row count, via
   `docker exec`.

## Out of scope

RDS provisioning, raising `INGEST_MAX_CHUNKS` (blocked on #52), 768/512 dims,
binary quantization, Alembic, and the other `docs/secrets-plan.md` §6 items
(dev DB `127.0.0.1` bind, t4g compose DB URL).

## Done when

- PR `feat/halfvec-default` → `dev` is open with Steps 1–5.
- `uv run pytest` passes without Doppler.
- Step 6 reproduced V2's metrics on a DB created from scratch by the app.
- The PR description lists the measured sizes and confirms no migration was
  needed anywhere.
