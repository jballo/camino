# Guided Tour Generation — Design & Plan (Phase 2)

Status: **end-to-end implemented.** The Plan → Retrieve → Draft → Review generator,
`TourJob` persistence, FastAPI journeys endpoints, and the Next.js frontend (journeys
proxy, `/generate` polling page, `/tours` library, and `/tours/{id}` reader) now all
exist. Remaining work is the M5 eval harness + failure-mode docs.

This doc is now both the Phase 2 design record and the current implementation tracker.
Earlier sections preserve the decisions; the milestones at the bottom show what has
landed and what is still open.

---

## 1. What we're building

A **guided tour** is a structured, ordered walkthrough of an ingested repo for a
given topic ("authentication flow", "request lifecycle"). Each step points at real
code — file, line range, exact snippet — and explains *what* it does and *why* it
exists that way.

Today the app can *answer questions* (`/explore` + ReAct agent), and the backend can
generate and persist grounded tours for already-ingested repos through
`/api/v1/journeys`. The frontend still cannot display or poll those tours: the
Next.js `/api/journeys` route remains a stub, and `/generate` / `/tours/{id}` are not
built yet.

### Where it fits

```mermaid
flowchart LR
  subgraph have["Already built"]
    Ingest[Ingest → chunks in pgvector]
    Search["hybrid_search()"]
    Schema["TourArtifact / TourStep<br/>app/models/tour.py"]
    Validate["validate_tour()<br/>eval/structural/validate.py"]
  end

  subgraph built["Built"]
    Pipeline[Tour generation pipeline]
    Persist[(tour_jobs table)]
    API[FastAPI journeys API + polling]
    Reader["Next.js proxy + /generate + /tours reader UI"]
  end

  subgraph remaining["Still pending"]
    Eval["LLM-as-judge eval + failure-mode docs (M5)"]
  end

  Search --> Pipeline
  Schema --> Pipeline
  Pipeline --> Validate
  Pipeline --> Persist
  Persist --> API
  API --> Reader
  Reader --> Eval

  style built fill:#f0fdf4,stroke:#16a34a
  style remaining fill:#fef3c7,stroke:#d97706
  style have fill:#f0fdf4,stroke:#16a34a
```

---

## 2. The data contract (exists — reuse as-is)

```6:27:Backend/app/models/tour.py
class TourStep(BaseModel):
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    snippet: str = Field(min_length=1)
    why: str | None = None
    ...

class TourArtifact(BaseModel):
    title: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    repo_name: str = Field(min_length=1)
    steps: list[TourStep] = Field(min_length=1)
```

This is the pipeline's output type and the reader UI's input type. No changes needed
to ship a first version.

---

## 3. The central design decision: snippets are *extracted*, never *generated*

The single biggest failure mode for a code-tour LLM is hallucinated citations —
inventing a `file_path`, wrong line numbers, or a snippet that doesn't match the
source. `validate_tour()` already catches these (`PATH_EXISTS`, `LINES_IN_BOUNDS`,
`SNIPPET_MATCHES`), but we'd rather **make them impossible by construction** than
detect-and-retry.

**Rule:** the LLM never writes a `snippet`, `file_path`, `start_line`, or `end_line`.
It only *selects* a retrieved chunk (by an opaque id we hand it) and optionally
narrows the line range within that chunk. We then derive the citation fields
deterministically from the stored `CodeChunkModel.source_code`:

- `file_path`, `start_line`, `end_line`, `language` ← chunk metadata
- `snippet` ← exact substring of the chunk's stored source for the chosen sub-range

The LLM authors only the prose fields it should own: `title`, `explanation`, `why`.

This means `SNIPPET_MATCHES` / `PATH_EXISTS` / `LINES_IN_BOUNDS` cannot fail for a
well-behaved generator. The Review node becomes a safety net, not the primary
grounding mechanism.

```mermaid
flowchart LR
  R["Retrieved chunks<br/>(SearchResult, has source_code + lines)"] --> L{LLM}
  L -->|writes prose| P["title / explanation / why"]
  L -->|selects id + optional sub-range| S[chunk id + line range]
  R --> D[Deterministic extractor]
  S --> D
  D -->|file_path, lines, snippet| Step[TourStep]
  P --> Step
```

---

## 4. Pipeline architecture (LangGraph)

Mirror the existing `app/agent/` structure (`graph.py`, `runner.py`, `tools.py`,
`state.py`) in a new `app/tour/` package. Four nodes, one bounded repair loop.

```mermaid
flowchart TB
  START([topic + repo]) --> PLAN

  PLAN["**Plan**<br/>LLM → ordered outline:<br/>N step intents + search queries"]
  RETRIEVE["**Retrieve**<br/>hybrid_search per step intent<br/>→ candidate chunks (dedup, cap)"]
  DRAFT["**Draft**<br/>LLM picks chunk id + range,<br/>writes title/explanation/why<br/>→ deterministic snippet extraction"]
  REVIEW{"**Review**<br/>validate_tour() + coverage checks"}
  DONE([TourArtifact])

  PLAN --> RETRIEVE --> DRAFT --> REVIEW
  REVIEW -->|valid| DONE
  REVIEW -->|issues & retries left| DRAFT
  REVIEW -->|out of retries| FAIL([partial / failed])

  style PLAN fill:#eff6ff,stroke:#3b82f6
  style RETRIEVE fill:#eff6ff,stroke:#3b82f6
  style DRAFT fill:#eff6ff,stroke:#3b82f6
  style REVIEW fill:#fef3c7,stroke:#d97706
```

### Node responsibilities

| Node | Input | Output | LLM? |
|---|---|---|---|
| **Plan** | topic, repo, (optional repo structure hints) | ordered list of `{step_intent, search_query}` | yes (structured output) |
| **Retrieve** | per-step queries | per-step candidate `SearchResult[]` (reuse `hybrid_search`, dedup by `chunk_id`) | no |
| **Draft** | step intent + candidate chunks | `TourStep` (prose from LLM, citation extracted) | yes (structured output) |
| **Review** | draft `TourArtifact` | `ValidationResult` + coverage flags | no |

### State

```python
# app/tour/state.py (sketch)
class TourState(TypedDict):
    topic: str
    repo_name: str
    installation_id: int
    plan: list[PlannedStep]          # step_intent + search_query
    candidates: dict[int, list[SearchResult]]  # plan index -> chunks
    steps: list[TourStep]
    issues: list[CheckIssue]
    attempts: int
```

### Key reuse

- `hybrid_search()` (`app/services/search.py`) — Retrieve node calls it directly, or
  wraps it in a tour-scoped tool like `build_hybrid_search_tool()`.
- `validate_tour_artifact()` (`eval/structural/validate.py`) — Review node. **Caveat
  below.**
- `ChatOpenAI` + structured output (`.with_structured_output(...)`) instead of the
  ReAct tool loop, since Plan and Draft have fixed shapes.

---

## 5. Validation at generation time (the one real gap)

`validate_tour_artifact()` checks steps against files **on disk** under a
`repo_root`. At generation time we have chunks **in Postgres**, not necessarily a
clone. Two options:

- **A — Validate against stored chunk source (recommended for v1).** Because snippets
  are extracted from `CodeChunkModel.source_code`, we can validate the snippet against
  the *same stored source* rather than a disk file. Add a
  `validate_tour_against_chunks(artifact, chunks_by_path)` sibling that mirrors the
  disk checks but reads from the DB. Keeps generation self-contained (no clone).
- **B — Keep a shallow clone from ingest and point `validate_tour()` at it.** Truer to
  "the repo as it exists," but couples generation to a working tree and re-clones.

Decision: **A for v1** (fast, no clone, and construction already guarantees grounding);
keep the disk-based `validate_tour()` as the offline/eval path. Revisit B if we want
to validate against files that were never chunked.

---

## 6. Persistence

Implemented in `app/models/tour_job.py`, following existing SQLModel conventions and
storing the artifact as JSONB so the schema can evolve without migrations.

```python
# app/models/tour_job.py
class TourJob(SQLModel, table=True):
    __tablename__ = "tour_jobs"
    id: int | None = Field(default=None, primary_key=True)
    userId: str = Field(index=True)
    installation_id: int
    repo_name: str = Field(index=True)
    topic: str
    status: str = Field(index=True)   # see state machine
    artifact: dict | None = Field(default=None, sa_column=Column(JSONB))
    error: str | None = None
    createdAt / updatedAt  # server_default=func.now(), onupdate=func.now()
```

### Job lifecycle

```mermaid
stateDiagram-v2
  [*] --> pending: POST /journeys
  pending --> generating: worker picks up
  generating --> complete: valid artifact persisted
  generating --> failed: unrecoverable / out of retries
  failed --> [*]
  complete --> [*]
```

For v1, "worker" is a FastAPI `BackgroundTasks` job in-process. The README already
plots SQS + a separate worker for Phase 3; the status column and polling API are
designed so that swap is invisible to the frontend.

---

## 7. API

Backend (`app/api/journeys.py`, wired in `app/main.py`), auth via the existing Clerk
`get_authenticated_user_id` dependency, same ownership check as `app/api/agent.py`.

| Method | Path | Body / result |
|---|---|---|
| `POST` | `/api/v1/journeys` | `{ repoName, topic, userId }` → `{ id, status: "pending" }` |
| `GET`  | `/api/v1/journeys/{id}` | `{ id, status, artifact?, error? }` |
| `GET`  | `/api/v1/journeys?repo=` | list a user's tours (for a library view, optional) |

### Request → render sequence

```mermaid
sequenceDiagram
  participant U as User (/)
  participant F as Next.js /api/journeys
  participant B as FastAPI /api/v1/journeys
  participant W as Worker (BackgroundTask)
  participant DB as Postgres

  U->>F: POST { url/repo, prompt/topic }
  F->>B: POST (Bearer Clerk JWT)
  B->>DB: insert TourJob(status=pending)
  B-->>F: { id, status: pending }
  F-->>U: redirect /generate?id=...
  B->>W: schedule generation(id)
  W->>W: Plan → Retrieve → Draft → Review
  W->>DB: update status=complete, artifact=...
  loop poll
    U->>F: GET /api/journeys/{id}
    F->>B: GET /api/v1/journeys/{id}
    B->>DB: read status
    B-->>U: status (+ artifact when complete)
  end
  U->>U: navigate /tours/{id}
```

---

## 8. Frontend

Implemented. Replaced the stub proxy and added the polling + reader + library pages,
reusing `/explore`'s markdown + `SourceCard` rendering patterns.

| Route | State today | Plan |
|---|---|---|
| `/api/journeys` (proxy) | done | `POST` create + `GET` list, forward Clerk JWT + inject `userId`, return backend JSON |
| `/api/journeys/[id]` (proxy) | done | `GET` poll one job (Bearer only) |
| `/` home form | done | POST `{ repoName, topic }` → read `{ id }` → route to `/generate?id=` |
| `/generate` | done | poll `GET /api/journeys/{id}` every 2s, show progress, redirect to reader on `complete`, show `error` on `failed` |
| `/tours` | done | library list of a user's jobs with status badges |
| `/tours/{id}` | done | reader: sticky TOC, per-step title/explanation/why, line-numbered snippet with `file_path:start-end` (plain `<pre>`; no highlighter dep) |

The stub was replaced by a real proxy (`POST` create + `GET` list) that forwards the
Clerk JWT and injects `userId`, mirroring `api/agent/ask`:

```18:31:Frontend/src/app/api/journeys/route.ts
    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

    const response = await fetch(`${backend_url}/api/v1/journeys`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        repoName,
        topic,
        userId,
      }),
    });
```

---

## 9. Evaluation

- **Structural eval (exists):** already validates fixtures. Add a fixture generated by
  the real pipeline once it runs, to lock the output shape.
- **LLM-as-judge (README todo):** faithfulness (does explanation match snippet?),
  relevance (does step serve the topic?), completeness (does the tour cover the
  topic?), ordering (do steps flow logically?). Run over a small topic set on the
  FastAPI hero repo.
- **Coverage check (cheap, no LLM):** every planned step produced a step; steps span
  ≥ N distinct files; no duplicate snippets.

---

## 10. Open questions / decisions to confirm

1. **Topic source** — free-text prompt only, or also auto-suggested topics from repo
   structure (README lists this as a separate todo)? *Assume free-text for v1.*
2. **Tour length** — fixed cap (e.g. 5–8 steps) or LLM-decided within a bound?
   *Assume bounded, model chooses within `[3, 8]`.*
3. **Model routing** — same `gpt-4o-mini` as the agent, or a stronger model for the
   Plan node? *Assume single model v1; leave a seam for cheap/expensive split.*
4. **Sync vs async** — `BackgroundTasks` in-process (v1) vs SQS worker (Phase 3).
   *Assume in-process with a polling API shaped for later SQS.*
5. **Validation target** — stored chunks (§5 option A) vs shallow clone (option B).
   *Assume A.*

---

## 11. Implementation milestones

- [x] **M1 — Generator core.** `app/tour/{state,schemas,prompts,extract,graph,runner}.py`;
      Plan → Retrieve → Draft over an already-ingested repo, with deterministic snippet
      extraction (grounding by construction). Unit tests in `tests/test_tour.py` (15,
      passing) and a live smoke eval `eval/run_tour_smoke_eval.py` that generates real
      tours and runs them through `validate_tour` (needs DB + `OPENAI_API_KEY`; not run
      here). Review/repair loop deferred to M2.
- [x] **M2 — Validation.** `validate_tour_against_chunks()` (`eval/structural/validate.py`,
      §5 option A) validates steps against stored chunk source, no clone. Review node
      (`app/tour/review.py`) combines those structural checks with coverage checks
      (every planned step produced, no duplicate citations, ≥ N distinct files) and is
      wired into the graph with a bounded Draft↔Review repair loop (`max_attempts`,
      default 2) that redrafts only flagged steps and stops early when remaining issues
      aren't repairable. `run_tour_smoke_eval.py --save-fixture NAME` persists a real
      generated artifact as a structural fixture + manifest entry to lock the output
      shape. Tests in `tests/test_tour.py` + `tests/test_structural_eval.py`.
- [x] **M3 — Persistence + API.** `TourJob` model + `tour_jobs` table
      (`app/models/tour_job.py`, artifact stored as JSONB, `TourJobStatus`
      pending→generating→complete/failed; int PK per existing SQLModel convention
      rather than the uuid sketch in §6). `app/api/journeys.py` wired at
      `/api/v1/journeys` in `main.py`: `POST` (create job + schedule background
      generation → `{id, status}`), `GET /{id}` (poll, with ownership check), and
      `GET ?repo=` (list a user's jobs). In-process `BackgroundTasks` runner
      (`_run_generation`) opens its own session, calls `generate_tour`, and persists
      `complete`+artifact or `failed`+error (catches `TourGenerationError`). Auth +
      `installationId` resolution mirror `app/api/agent.py`. Route tests in
      `tests/test_journeys_route.py` (11, passing).
- [x] **M4 — Frontend.** Real `/api/journeys` proxy (`POST` create + `GET` list) and
      `/api/journeys/[id]` (`GET` poll), all forwarding the Clerk JWT and injecting
      `userId`, mirroring `api/agent/ask`. Home form now sends `{ repoName, topic }`,
      reads `{ id }`, and routes to `/generate?id=`. `/generate` polls
      `GET /api/journeys/{id}` every 2s with a pending→generating→complete progress
      indicator, redirects to the reader on `complete`, and surfaces `error` on
      `failed`. `/tours/{id}` reader renders the `TourArtifact`: sticky TOC, per-step
      title/explanation (markdown)/why callout, and a citation block with
      `file_path:start-end` + line-numbered snippet (plain `<pre>`; no highlighter dep
      added). `/tours` library lists a user's jobs with status badges. Shared TS types
      in `src/types/tour.ts`. `tsc --noEmit` and `eslint` clean.
- [ ] **M5 — Eval + docs.** LLM-as-judge harness; known limitations and failure
      modes.

Milestones are independently shippable: M1–M2 make the pipeline runnable from a
script/eval; M3–M4 expose it in the product.
