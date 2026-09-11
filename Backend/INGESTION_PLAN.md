# Staged-Generation Repository Ingestion — Implementation Plan

## Goal

Fix the unbounded-memory ingestion pipeline. Today `ingest_repository` holds every
parsed chunk, every embedding text, every vector, and every ORM object for the whole
repository in RAM at once. The redesign processes the repo in small waves, committing
each wave to Postgres and freeing it from memory, while guaranteeing that searches
never see duplicate, partial, or mixed results — even mid-re-ingest. A hard cap on
chunks per repository rides along.

## Design summary

| Component | What it is | Its one job |
|---|---|---|
| `generation` column on `code_chunks` | Value minted per ingest run (UUID hex) | Labels rows so two versions of a repo's index can coexist without confusion |
| `repo_index_state` table (registry) | One row per `(installation_id, repo_name)` with `active_generation` | The pointer that says which generation is live; flipping it is the atomic publish |
| `live_code_chunks` view | Saved query joining `code_chunks` to the registry | Bakes the "only active generation" filter in one place so read queries can't forget it |
| Batched pipeline | parse wave → embed wave → insert + commit → free memory | Bounds peak memory to one wave |
| Swap transaction | Upsert registry + delete old-generation rows, in one small commit | Atomic switchover; no external API calls inside it |
| Cleanup step | Delete non-active-generation rows at job start | Sweeps debris from crashed/abandoned runs |
| Chunk cap | `ingest_max_chunks` setting, enforced as waves stream | Fails oversized repos early with a permanent (non-retried) error |

Failure model: if an ingest dies at wave 7 of 20, the registry still points at the old
generation, users keep searching the complete old index, and the orphaned new-generation
rows are deleted when the retry starts. Nothing is ever half-visible.

---

## 1. Config — `app/config.py`

Add two settings:

```python
ingest_wave_chunks: int = Field(default=256, gt=0)   # chunks per parse→embed→insert wave
ingest_max_chunks: int = Field(default=25_000, gt=0) # hard cap per repository
```

- `256` aligns with `BATCH_SIZE` in `app/services/embeddings.py` (one OpenAI call per wave).
- Document both in `Backend/.env.example` and `Backend/README.md`.

## 2. Models — `app/models/code.py`

**`CodeChunkModel`:**
- Add `generation: str = Field(index=False)` (indexed via the composite index below, not alone).
- Replace `uq_chunk_identity` with a constraint that includes `generation`:
  `UniqueConstraint("installation_id", "repo_name", "generation", "file_path", "symbol_name", "start_line", name="uq_chunk_identity_gen")`.
  Without this, a re-ingest collides with the still-live old copy of every symbol.
- `from_parsed(...)` gains a `generation: str` keyword argument.

**New `RepoIndexState` model** (same file or `app/models/repo_index_state.py`):

```python
class RepoIndexState(SQLModel, table=True):
    __tablename__ = "repo_index_state"
    __table_args__ = (
        UniqueConstraint("installation_id", "repo_name", name="uq_repo_index_state"),
    )
    id: int | None = Field(default=None, primary_key=True)
    installation_id: int = Field(index=True)
    repo_name: str
    active_generation: str
```

## 3. Migration — `app/main.py` lifespan

The project evolves schema with idempotent SQL in the lifespan (see the existing
`ALTER TABLE ... IF NOT EXISTS` block), so follow that pattern. Order matters;
run after `SQLModel.metadata.create_all(engine)` (which creates `repo_index_state`):

```sql
-- 1. Add the column, nullable at first so existing rows survive.
ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS generation TEXT;

-- 2. Backfill existing rows into a synthetic "legacy" generation.
UPDATE code_chunks SET generation = 'legacy' WHERE generation IS NULL;

-- 3. Now enforce NOT NULL (idempotent).
ALTER TABLE code_chunks ALTER COLUMN generation SET NOT NULL;

-- 4. Register every existing repo as live at generation 'legacy'.
--    CRITICAL: without this backfill, all existing data vanishes from the view.
INSERT INTO repo_index_state (installation_id, repo_name, active_generation)
SELECT DISTINCT installation_id, repo_name, 'legacy' FROM code_chunks
ON CONFLICT (installation_id, repo_name) DO NOTHING;

-- 5. Swap the unique constraint to include generation.
ALTER TABLE code_chunks DROP CONSTRAINT IF EXISTS uq_chunk_identity;
CREATE UNIQUE INDEX IF NOT EXISTS uq_chunk_identity_gen
ON code_chunks (installation_id, repo_name, generation, file_path, symbol_name, start_line);

-- 6. Composite index for the per-repo, per-generation lookups the new
--    queries and cleanup deletes will do constantly.
CREATE INDEX IF NOT EXISTS ix_chunks_repo_generation
ON code_chunks (installation_id, repo_name, generation);

-- 7. The view every read path uses.
CREATE OR REPLACE VIEW live_code_chunks AS
SELECT c.*
FROM code_chunks c
JOIN repo_index_state s
  ON s.installation_id = c.installation_id
 AND s.repo_name = c.repo_name
 AND s.active_generation = c.generation;
```

Note: `CREATE OR REPLACE VIEW` fails if the column set of `code_chunks` later changes
shape; if that ever happens, `DROP VIEW IF EXISTS` + `CREATE` instead.

## 4. Pipeline rewrite — `app/services/repository_ingestion.py`

### Structural change

`_walk_repository` currently downloads, extracts, parses everything, and returns all
chunks — the temp directory closes when it returns. Split it:

- `_prepare_repository(repo_name, installation_id, temp_path) -> tuple[Path, str]` —
  auth, tarball download, extraction (sync, run via `asyncio.to_thread`). Returns
  `(repo_root, commit_sha)`.
- `_iter_file_chunks(repo_root) -> Iterator[list[CodeChunk]]` — the existing walk/parse
  logic, but yielding chunks file-by-file instead of accumulating.

`ingest_repository` owns the `TemporaryDirectory` context so the extracted tree stays
on disk for the whole batching loop.

### New flow

```
1. generation = uuid4().hex

2. CLEANUP (own transaction):
   DELETE FROM code_chunks
   WHERE repo_name/installation match
     AND generation != (registry's active_generation, or delete all if no registry row)
   commit.  # sweeps debris from crashed prior runs

3. Download + extract (thread). Existing tarball-size cap stays as-is.

4. WAVE LOOP — accumulate parsed chunks (parse in thread) until >= ingest_wave_chunks:
   a. total_chunks += len(wave)
      if total_chunks > settings.ingest_max_chunks:
          raise PermanentRepositoryIngestionError(
              "Repository exceeds the maximum indexable size")
      # permanent => worker won't retry; cleanup on any later run removes staged rows
   b. vectors = await embed_all([build_embedding_text(c) for c in wave])
   c. build CodeChunkModel rows (with generation=generation) + session.add_all
      session.flush()
      build CodeChunkEmbedding rows from flushed ids + session.add_all
      session.commit()
   d. drop all references to the wave's chunks/texts/vectors/models
   e. log wave progress (wave number, cumulative chunks, elapsed)

5. SEARCH VECTOR (own transaction):
   run populate_search_vector_sql scoped to this generation (see §5), commit.

6. SWAP (one small transaction — this is the atomic publish):
   INSERT INTO repo_index_state (installation_id, repo_name, active_generation)
   VALUES (...) ON CONFLICT (installation_id, repo_name)
   DO UPDATE SET active_generation = EXCLUDED.active_generation;

   DELETE FROM code_chunks
   WHERE repo_name/installation match AND generation != :generation;
   -- embeddings cascade via the existing ON DELETE CASCADE FK
   commit.
```

### Error handling

- Keep the existing exception-to-`Transient`/`Permanent` mapping and phase logging;
  add phases `cleanup`, `wave`, `swap`.
- On any failure, `session.rollback()` as today. Already-committed staged waves are
  *not* rolled back — they're invisible (wrong generation) and removed by the next
  run's cleanup. Optionally add a best-effort staged-row delete in the error path.
- Lease-loss semantics are unchanged from today: the swap commit is the analogue of
  the current single commit.

### Worker note — `app/worker.py`

No changes required. `run_job` opens a dedicated session per job and all job-row
status writes go through separate guarded UPDATE + commit calls, so the ingest's
per-wave commits cannot flush unrelated pending state. Verify this assumption holds
when implementing.

## 5. Search-vector scoping — `app/services/search_index.py`

`populate_search_vector_sql` currently scopes by repo + installation only; during a
staged ingest that matches both generations.

- Add a `generation` bind param to the UPDATE's WHERE clause.
- Ingest path (`only_null=True`): pass the new generation.
- `rebuild_search_vector` (`only_null=False`, ops tool): read the repo's
  `active_generation` from the registry first and pass it.

## 6. Read paths — point at the view

Three queries change `code_chunks` → `live_code_chunks`; nothing else about them changes:

1. `_vector_search` in `app/services/search.py` — the `JOIN code_chunks c` becomes
   `JOIN live_code_chunks c`.
2. `_fts_search` in `app/services/search.py` — `FROM code_chunks c, q` becomes
   `FROM live_code_chunks c, q`.
3. The per-repo chunk-count query in `app/api/repositories.py` (currently an ORM
   `select(CodeChunkModel...)`) — switch to a small `text()` query against
   `live_code_chunks`, or keep the ORM and join `RepoIndexState` on
   `active_generation == CodeChunkModel.generation`.

`_demote_paths` and `_load_chunks` fetch by chunk id and those ids come from the two
filtered queries, so they may stay on the raw table. (Switching them to the view too
is harmless and more future-proof — implementer's choice.)

**Convention going forward:** every new read of chunk data uses `live_code_chunks`;
the raw table is for ingestion and deletion code only.

## 7. Deletion services

`delete_installation_local_data` (`app/services/installation_deletion.py`) and the
account-deletion equivalent (`app/services/account_deletion.py`) delete
`CodeChunkModel` rows by installation. Add a matching delete of `RepoIndexState`
rows so registry entries don't leak after uninstall/account deletion.

## 8. Tests — `Backend/tests/test_repository_ingestion.py` (+ additions)

Update existing tests for the new flow, and add:

- **Waves commit incrementally**: repo producing > `ingest_wave_chunks` chunks results
  in multiple `embed` calls and multiple commits; final counts correct.
- **Chunk cap**: repo exceeding `ingest_max_chunks` raises
  `PermanentRepositoryIngestionError`; registry unchanged; view still serves old data.
- **Crash mid-ingest**: make embedding fail on wave 2; assert registry still points at
  old generation, `live_code_chunks` returns only old rows, and a subsequent successful
  run cleans staged rows and publishes exactly one generation.
- **Swap correctness**: after success, view returns only new-generation rows; old rows
  physically deleted (including cascaded embeddings).
- **Re-ingest identity**: same symbol in old + new generation does not violate the
  unique index.
- **First ingest**: no registry row → view empty until the swap commits.
- **Deletion services**: registry rows removed with chunks.
- **Search queries**: `_vector_search` / `_fts_search` only surface active-generation
  chunks when two generations are present.

## 9. Docs

- `Backend/README.md`: document the staged-generation design (a paragraph), the two new
  env vars, and the `live_code_chunks` convention for future queries.
- Root `README.md`: the pre-deployment task about capping ingestion size can be marked
  done once this lands.

## Rollout

Single deploy is safe: the lifespan migration (backfill + view) runs before the worker
starts claiming jobs, and every statement is idempotent. Existing indexed repos keep
serving from generation `'legacy'` until their next re-ingest naturally replaces it.

## Defaults to confirm before building

- `ingest_wave_chunks = 256` — one OpenAI embeddings call per wave; raise to lower
  request count, lower to shrink peak memory further.
- `ingest_max_chunks = 25_000` — at ~256 chunks/call this is ~100 embedding calls and
  roughly 150 MB of vector data in Postgres per repo; tune to taste.
- Generation id source: `uuid4().hex`. (Job id would also work but couples the schema
  to the jobs table.)
