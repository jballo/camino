# Retrieval experiment log

Durable, version-controlled record of the retrieval-improvement loop so results
survive beyond any single working session. Raw per-run output lives in
`eval/runs/<label>.json` (gitignored); this file is the curated, committed
summary used to compare experiments and decide which changes **stack**.

How to use:

1. Run an experiment: `uv run python -m eval.run_eval --label <id> --out eval/runs/<id>.json`
   (see `README.md` for flags). Use `--mode ablation` to see per-retriever contribution.
2. Add a row to the **Leaderboard** and fill in the experiment's detail section.
3. Update the **Per-question outcome matrix** so we can see which experiments fix
   which questions — this is how we spot complementary pairings.
4. Decide `kept?` and note whether it requires a re-ingest (embedding change) or is
   query-time only (cheap).

---

## Continue here (handoff for a new chat)

**Current shipped stack:** exp1 (FTS) + exp3 (post-fusion demotion + top_n=60) +
exp4 (embedding text) + exp5 (retrieval-time demo-path filter). Optional exp6
(cross-encoder rerank, `--rerank`, not default in prod).

**Best numbers (FastAPI golden set, k=5, limit=10):** hit **0.950**, recall **0.925**
(exp6 + BGE reranker blend). Shipped defaults (no rerank): hit **0.900**, recall
**0.858**, MRR **0.766**. Baseline: 0.800 / 0.767 / 0.649.

**Still missing (1/20):**

| id | question | labeled chunks | ranks (vector / fts / final) |
|---|---|---|---|
| q03 | path param validation on route | `routing.py:APIRoute`, `routing.py:get_request_handler` | 20 / 17 / **9**; handler in FTS@6 but not top-5 |

**Reproduce shipped eval (needs DB + OpenAI):**

```bash
cd Backend
uv run python -m eval.ingest_local          # first time: clone + ingest + embed
uv run python -m eval.ingest_local --rebuild-fts   # after FTS tokenization changes only
uv run python -m eval.run_eval --label shipped --out eval/runs/shipped.json
# A/B retrieval filter off:
uv run python -m eval.run_eval --no-filter-demo-paths --label no_filter
# Ablation vector vs fts vs hybrid:
uv run python -m eval.run_eval --mode ablation
```

**Committed reference:** `eval/baseline_results.json` (original baseline, never overwritten).
**Local run artifacts:** `eval/runs/*.json` (gitignored).

---

## Metrics & caveats

- Track **hit_rate@5, recall@5, MRR**. Dataset is 20 questions, FastAPI 0.115.6.
- **Ignore precision@5**: 18/20 questions have a single relevant chunk, so the
  ceiling for precision@5 is 0.2. It carries no signal here.
- **Tiebreak:** FTS and vector retrievers use `, c.id` as a deterministic tiebreak
  in `ORDER BY`. Residual run-to-run jitter should be small (±0.01 on MRR).

---

## Baseline (FastAPI 0.115.6, k=5, limit=10, hybrid, equal RRF weights)

| Metric | Value |
|---|---|
| hit_rate@5 | 0.800 |
| recall@5 | 0.767 |
| precision@5 | 0.170 (capped, ignore) |
| MRR | 0.649 |

Stored config: `mode=hybrid top_n=20 rrf_k=60 vector_weight=1.0 fts_weight=1.0`.

---

## Leaderboard

| exp | change | type | hit@5 | recall@5 | MRR | kept? | run file |
|---|---|---|---|---|---|---|---|
| baseline | as shipped | — | 0.800 | 0.767 | 0.649 | n/a | `runs/baseline_repro.json` |
| exp0 | instrument harness (no-op refactor) | query | 0.800 | 0.767 | 0.649 | yes | `runs/baseline_repro.json` |
| exp1 | fix FTS (tokenize + OR query), equal weight | ingest* | 0.700 | 0.642 | 0.522 | partial | `runs/exp1_hybrid.json` |
| exp1+w | exp1 + fts_weight=0.2 | query | 0.750 | 0.667 | 0.595 | no | |
| **exp1+3** | **exp1 + path_penalty=0.3 + top_n=60** | query | **0.850** | **0.792** | **0.782** | **yes (shipped)** | `runs/exp3_best.json` |
| exp1+3 (top_n=20) | exp1 + path_penalty=0.3 | query | 0.800 | 0.742 | 0.757 | no | |
| exp2 | FTS weight sweep (on top of exp1+3) | query | 0.850 | 0.792 | 0.736–0.782 | partial | |
| **exp4** | **NL header + class methods in embedding text** | ingest | **0.850** | **0.850** | 0.773 | **yes (shipped)** | `runs/exp4.json` |
| **exp5** | **retrieval-time demo-path filter** | query | **0.900** | **0.858** | 0.766 | **yes (shipped)** | `runs/exp5.json` |
| exp6 | cross-encoder rerank (MiniLM blend rrf_w=0.9) | query | 0.900 | **0.875** | **0.839** | partial | `runs/exp6.json` |
| **exp6+bge** | **BGE reranker blend rrf_w=0.9** | query | **0.950** | **0.925** | 0.817 | partial | |
| exp4 (no filter) | exp4 config, filter off (A/B) | query | 0.850 | 0.850 | 0.748 | — | |

**Shipped defaults**: `top_n=60`, `path_penalty=0.3`, `filter_demo_paths=True`,
equal RRF weights (`search.py`) + enriched `build_embedding_text` (`embeddings.py`).
Cumulative vs baseline: **hit 0.800→0.900, recall 0.767→0.858, MRR
0.649→0.766** (+0.10 / +0.091 / +0.117).

\*exp1 rebuilds `search_vector` via SQL `UPDATE` only — **no re-embedding**.

`type` = `query` (no re-ingest, cheap, instantly comparable) vs `ingest`
(mutates the index; A/B under a separate `installation_id` to preserve baseline).

### Shipped config snapshot

| knob | value | where |
|---|---|---|
| `top_n` | 60 | `search.py` `DEFAULT_TOP_N` |
| `rrf_k` | 60 | `search.py` `DEFAULT_K` |
| `vector_weight` / `fts_weight` | 1.0 / 1.0 | equal RRF (exp2 showed no gain from tuning) |
| `path_penalty` | 0.3 | `search.py` `DEFAULT_PATH_PENALTY` |
| `filter_demo_paths` | True | `search.py` `DEFAULT_FILTER_DEMO_PATHS` |
| `rerank` | False | `search.py` `DEFAULT_RERANK` (exp6 optional) |
| `rerank_top_n` / `rerank_rrf_weight` | 30 / 0.9 | `search.py`, `rerank.py` |
| `rerank_model` | `BAAI/bge-reranker-base` | `rerank.py` `RERANK_MODEL` (exp6 optional) |
| `limit` (final hydrate) | 10 | `search.py` `DEFAULT_FINAL_LIMIT` |
| FTS index | split identifiers + OR query | `search_index.py`, `_fts_search` |
| embedding text | NL header + class methods | `embeddings.py` `build_embedding_text` |

Demo-path markers (filter + demotion): `tests/`, `test_`, `docs_src/`, `tutorial`,
`examples/` in `file_path`.

### Code map (files touched by experiments)

| file | exps | role |
|---|---|---|
| `app/services/search.py` | 0–3, 5, 6 | hybrid search, RRF, FTS query, demotion, retrieval filter, rerank hook |
| `app/services/rerank.py` | 6 | cross-encoder rerank stage |
| `app/services/search_index.py` | 1 | canonical `search_vector` SQL + `--rebuild-fts` helper |
| `app/services/embeddings.py` | 4 | `build_embedding_text` enrichment |
| `app/services/parser.py` | — | unchanged; chunk boundaries still default tree-sitter |
| `app/api/repositories.py` | 1 | prod ingest uses shared `search_vector` expr |
| `eval/run_eval.py` | 0–6 | harness, flags, ablation, diagnostics |
| `eval/ingest_local.py` | 1 | eval ingest + `--rebuild-fts` |
| `eval/golden_dataset.json` | — | 20 hand-labeled Qs, FastAPI 0.115.6 |
| `eval/baseline_results.json` | — | frozen baseline metrics + per-question rows |
| `eval/README.md` | 0+ | reproduce instructions |

---

## Per-question outcome matrix

`Y` = ≥1 relevant chunk in top-5. `(F)` = FTS-only also hits (exp1 ablation).
`↓` = lost vs baseline; `↑` = gained vs baseline. `@N` = best relevant chunk at
final rank N (miss). `rec` = recall when <1.0 or notably changed.

| q | question (short) | n_rel | baseline | exp1 | exp1+3 | exp4 | exp5 ★ | exp6 |
|---|---|---|---|---|---|---|---|---|
| q01 | dependency injection | 3 | Y (.33) | Y (F) | Y (.33) | Y (1.0) | Y (.67) | Y (.67) |
| q02 | OpenAPI schema generated | 1 | **.** | **.** | **.** | **.** | **Y ↑** | Y |
| q03 | path param validation | 2 | **.** | **.** | **.** | **.** | **.** (@9) | **.** (@9) |
| q04 | objects → JSON | 1 | Y | Y | Y | Y | Y | Y |
| q05 | where is Depends | 2 | **.** | Y (F) | Y | Y (1.0) | Y (.5) | Y (.5) |
| q06 | OAuth2 password bearer | 1 | Y | Y | Y | Y | Y | Y |
| q07 | request validation → HTTP | 1 | Y | Y | Y | Y | Y | Y |
| q08 | main FastAPI app class | 1 | Y | Y (F) | Y | Y | Y | Y |
| q09 | uploaded files | 1 | Y | . ↓ | Y | Y | Y | Y |
| q10 | serialize response model | 1 | **.** | **.** | Y ↑ | Y | Y | Y |
| q11 | Swagger UI HTML | 1 | Y | Y (F) | Y | Y | Y | Y |
| q12 | OAuth2 password form | 1 | Y | Y (F) | Y | Y | Y | Y |
| q13 | API key header auth | 1 | Y | Y (F) | Y | Y | Y | Y |
| q14 | HTTP exceptions | 2 | Y | Y (F) | Y | Y | Y | Y |
| q15 | form field parameter | 1 | Y | . ↓ | Y | Y | Y | Y |
| q16 | APIRouter / sub-routers | 1 | Y | Y | Y | Y | Y | Y |
| q17 | websocket routes | 1 | Y | . ↓ | . ↓ | . ↓ | **.** (@6) | **.** (@7) |
| q18 | path op in OpenAPI | 1 | Y | Y | Y | Y | Y | Y |
| q19 | HTTP bearer token | 1 | Y | Y | Y | Y | Y | Y |
| q20 | background tasks | 1 | Y | Y (F) | Y | Y | Y | Y |

★ exp5 = current shipped defaults (exp1+3+4+5). exp6 = exp5 + optional rerank blend.

**Miss progression:** baseline q02,q03,q05,q10 → exp1+3 adds q05,q10, loses q17 →
exp5 adds q02; still q03,q17.

---

## EXP 0 — Instrument the harness (DONE, kept)

**Goal:** make every later experiment measurable and reproducible.

**Changes:**
- `search.py`: weighted RRF (`_rrf_fuse(weights=…)`); `hybrid_search_debug()` core
  returning per-retriever ranks + supporting `mode` (hybrid/vector/fts) and
  `vector_weight`/`fts_weight`. `hybrid_search()` now wraps it (defaults unchanged).
- `run_eval.py`: all knobs are flags and recorded in output `config`; `--mode
  ablation`; per-chunk attribution (rank in vector list / FTS list / fused);
  `NOT INDEXED` flag for labeled chunks missing from the index; `--label`/`--out`.

**Verification:** hybrid run reproduced baseline exactly (0.800/0.767/0.649) →
refactor is a confirmed no-op.

### Key findings (this is what drives Exp 1–4)

**1. FTS is effectively dead** — ablation:

| mode | hit_rate@5 | recall@5 | MRR |
|---|---|---|---|
| vector | 0.800 | 0.742 | 0.624 |
| fts | **0.050** | **0.025** | **0.050** |
| hybrid | 0.800 | 0.767 | 0.649 |

FTS hits only q14. "Hybrid" is currently ≈ vector-only. Root causes:
- **`plainto_tsquery` AND-semantics**: NL question → all stemmed tokens required
  (`openapi & schema & generat`); sparse code chunks rarely match → empty lists.
- **Identifiers not tokenized**: `symbol_name` indexed with `'simple'`, so
  `get_openapi` / `OAuth2PasswordBearer` are single opaque tokens; NL words never
  match them.
- **Stemming mismatch**: query `'english'` ("Depends"→"depend") vs symbol
  `'simple'` ("depends") — exact-symbol queries (q05) silently fail.

**2. Vector-depth of the misses** (probe: `--mode vector --top-n 300`):

| chunk | q | vector rank | implication |
|---|---|---|---|
| `get_openapi` | q02 | >300 (invisible) | only FTS can find it |
| `get_request_handler` | q03 | >300 (invisible) | only FTS can find it |
| `APIRoute` | q03 | 59 | FTS lift via fusion |
| `serialize_response` | q10 | ~21–50 | fusion lift / bigger top_n |
| `Depends` (param_functions) | q05 | 17 | FTS lift via fusion |

No `NOT INDEXED` warnings → parser/dataset are fine; all labeled chunks exist.

**Conclusion:** fixing FTS (Exp 1) is the highest-leverage *and* cheapest change
(SQL rebuild, no re-embed). Two misses are vector-invisible and *require* FTS;
three more are deep vector hits that a real FTS signal lifts via fusion.

---

## EXP 1 — Fix FTS (DONE, partial — needs exp2+exp3 to net positive)

**Changes shipped:**
- New `app/services/search_index.py` centralizes the `search_vector` expression
  (used by prod ingest, eval ingest, and rebuild). Splits snake_case + camelCase
  + acronym boundaries (`OAuth2PasswordBearer` → oauth2/password/bearer,
  `get_openapi` → get/openapi), indexed with `english`; also keeps the raw
  `simple` lexeme for exact symbol lookups.
- `_fts_search`: AND→OR query (`replace(plainto_tsquery(...)::text,' & ',' | ')`)
  so any-term matches are candidates, ranked by `ts_rank`; added `c.id`
  deterministic tiebreak.
- `eval.ingest_local --rebuild-fts`: recompute `search_vector` via SQL UPDATE,
  no re-embed (ran in ~1.5s).

**Result — ablation (equal RRF weight):**

| mode | hit_rate@5 | recall@5 | MRR | vs baseline |
|---|---|---|---|---|
| fts | **0.400** | 0.317 | 0.290 | **was 0.050** — FTS is alive |
| hybrid | 0.700 | 0.642 | 0.522 | ↓ from 0.800 |

**Interpretation (key for stacking):**
- FTS went from dead (0.05) to a real signal (0.40). It now *independently*
  solves **q05** ("where is Depends defined") — the exact-symbol win — and fires
  on many symbol-named queries (marked `(F)` in the matrix).
- BUT equal-weight hybrid **regressed** (0.80→0.70): the now-functional FTS is
  *noisy* (OR matches common words), and at weight 1.0 it injects test/tutorial
  chunks that knock vector-only wins (**q09, q15, q17**) out of the fused top-5.
- `fts_weight` sweep recovers only partially: 0.5→0.70, 0.3→0.70, **0.2→0.750**.
  Weight alone can't beat baseline because the noise is structural (tests/
  tutorials), not just magnitude.

**Conclusion:** Exp 1 is necessary infrastructure (FTS works + exact-symbol hits)
but **net-negative in isolation** (hybrid 0.70). Must stack with exp3 (demotion +
top_n) — see exp1+3 row on leaderboard. Shipped with exp3+4+5.

**Artifacts:** `runs/exp1_ablation.json`, `runs/exp1_hybrid.json`.

## EXP 3 — Demote tests/tutorials (DONE, kept — run before exp2)

**Change shipped:** `_demote_paths` multiplies the fused RRF score of chunks
whose `file_path` contains any of `DEMOTE_PATH_SUBSTRINGS` (`tests/`, `test_`,
`docs_src/`, `tutorial`, `examples/`) by `path_penalty`, applied to the full
fused list before the top-k cut. Exposed as `--path-penalty` and the
`hybrid_search(path_penalty=…)` kwarg. Query-time only (no re-ingest).

**Result (on top of exp1's fixed FTS):**

| config | hit@5 | recall@5 | MRR |
|---|---|---|---|
| exp1 alone (penalty=1.0, top_n=20) | 0.700 | 0.642 | 0.522 |
| + penalty=0.3 (top_n=20) | 0.800 | 0.742 | 0.757 |
| + penalty=0.3, **top_n=60** (shipped) | **0.850** | **0.792** | **0.782** |

Penalty value barely matters (0.5/0.3/0.1 all ≈ same) — demotion is a step
function once tests fall below internals. The **top_n=20→60 bump** is what
unlocks the extra hit_rate (q03 `get_request_handler` → final rank 6; deep
vector hits reach fusion).

**Interpretation:** Exp 3 is the decisive partner for Exp 1 — it removes the
structural FTS noise (tests/tutorials), turning the Exp 1 regression into a net
win. MRR gains come from demotion promoting already-correct hits whose rank was
suppressed by test/tutorial chunks (q09/q11/q20 → rr 1.0).

**Residual / regression:** q17 (`APIWebSocketRoute`) drops from baseline rank 5
to rank 7 — at top_n=60 the larger FTS candidate pool dilutes a strong vector
hit (vector rank 5). Candidate for the reranker / per-query top_n later.

**Artifacts:** `runs/exp3_best.json`.

## EXP 2 — Weighted RRF (DONE as a sweep on top of exp1+3; low value)

Swept `--fts-weight` ∈ {0.5,1.0,1.5,2.0} at penalty=0.3, top_n=60. hit_rate and
recall are flat at 0.850/0.792 across the range; MRR is best at **equal weight
(1.0 → 0.782)** and degrades as FTS is up- or down-weighted (1.5/2.0 → 0.736).

**Conclusion:** once tests are demoted (exp3) and top_n is raised, FTS weighting
adds nothing — equal weight is optimal. Exp 2 is effectively *subsumed* by
exp3+top_n. Not shipped as a separate knob change.

## EXP 4 — Better embedding text (DONE, kept — requires re-ingest)

**Diagnosis first:** the vector-invisible chunks all have **no docstring** and
are large (APIRoute 164 lines, get_request_handler 140, get_openapi 92). The old
`build_embedding_text` = signature (mostly param plumbing) + 15 body lines, so
there was no text expressing what the symbol *does*.

**Change shipped** (`build_embedding_text` in `embeddings.py`):
- Prepend an NL header: `"{symbol_type} {humanized_name} ({humanized_path})"`,
  e.g. `function get openapi (fastapi openapi utils)`,
  `class API Route (fastapi routing)`.
- For classes, append `methods: a, b, c` (the API surface) instead of relying on
  the first lines of `__init__`.
- Re-ingested the fixture (re-embed 5129 chunks, ~47s).

**Result (shipped config, vs exp1+3):**

| metric | exp1+3 | exp4 | Δ |
|---|---|---|---|
| hit_rate@5 | 0.850 | 0.850 | 0 |
| recall@5 | 0.792 | **0.850** | **+0.058** |
| MRR | 0.782 | 0.773 | −0.009 (noise) |

Recall win is real and concentrated on multi-relevant questions: q01 .33→1.0
(now finds `solve_dependencies` + `get_dependant` + `Depends`), q05 .5→1.0.

**What it did NOT fix — the key finding:** q02/q03 are still missed. Vector-depth
probe (top_n=500) after re-embed: `get_openapi` at rank **328**, `APIRoute` at
**184** (≫ top_n=60). The enriched text didn't crack the top of vector space
because hundreds of test/tutorial chunks still rank above the internals. Since
demotion is applied *post-fusion* (only to the retrieved top_n pool), it can't
help a chunk that never enters the pool. **The blocker is retrieval-time
crowding, not embedding quality.**

## EXP 5 — Retrieval-time demo-path filtering (DONE, kept — shipped)

**Hypothesis confirmed:** post-fusion demotion (exp3) can't help chunks that never
enter the top_n candidate pool. Excluding test/tutorial paths *inside*
`_vector_search` and `_fts_search` lets library internals fill those slots.

**Change shipped** (`search.py`):
- `_demo_path_exclusion_sql()` — same markers as `DEMOTE_PATH_SUBSTRINGS`,
  applied as `AND NOT (POSITION(...) > 0 OR ...)` in both retrievers.
- `filter_demo_paths=True` default on `hybrid_search` / `hybrid_search_debug`.
- Post-fusion `_demote_paths` kept as a safety net for edge-case paths.
- Eval flag: `--no-filter-demo-paths` to disable for A/B.

**Result (exp4 config + filter on vs filter off):**

| config | hit@5 | recall@5 | MRR |
|---|---|---|---|
| exp4 (filter off) | 0.850 | 0.850 | 0.748 |
| **exp5 (filter on)** | **0.900** | **0.858** | 0.766 |

**+0.05 hit** — fixes **q02** (`get_openapi` now hits at rr=0.20). Confirms exp4
diagnosis: overall vector rank was 328, but rank among non-test chunks is good
enough to enter top_n=60 once tests are excluded.

**Residual:** q03 still misses — `APIRoute` at final rank 9, `get_request_handler`
in FTS pool (rank 6) but not fused into top-5. q17 still misses (`APIWebSocketRoute`
final rank 6, improved from 8). q01 recall dips .67 vs exp4's 1.0 (one labeled
chunk displaced). Candidate for reranker / larger final limit.

**Artifacts:** `runs/exp5.json`.

## EXP 6 — Cross-encoder reranker (DONE, partial — not shipped as default)

**Hypothesis:** q03/q17 fail because RRF can't promote the right chunk from a
mixed pool even when vector/fts both surface it (q03: APIRoute vector@20, fts@17;
q17: vector@4 but final@6).

**Change shipped:**
- New `app/services/rerank.py` — `cross-encoder/ms-marco-MiniLM-L-6-v2` scores
  `(query, chunk_text)` pairs after RRF fusion; chunk text = symbol + path +
  signature + docstring + body preview.
- `search.py` — optional final stage: hydrate `rerank_top_n` fused candidates,
  rerank, cut to `limit`. Knobs: `rerank`, `rerank_top_n`, `rerank_rrf_weight`,
  `rerank_model`. Default `rerank=False` (no prod latency change).
- Blended score: `rerank_rrf_weight * norm(RRF) + (1-w) * norm(CE)` — pure CE
  regresses because the model favors generic OpenAPI helpers over `get_openapi`.
- Eval flags: `--rerank`, `--rerank-top-n`, `--rerank-rrf-weight`, `--rerank-model`.
- Dependency: `sentence-transformers` in `pyproject.toml`.

**Result (exp5 config + rerank on vs off):**

| config | hit@5 | recall@5 | MRR |
|---|---|---|---|
| exp5 (rerank off) | **0.900** | 0.858 | 0.766 |
| exp6 pure CE (rrf_w=0.0) | 0.850 ↓ | 0.792 | 0.775 |
| **exp6 blend MiniLM (rrf_w=0.9)** | **0.900** | **0.875** | **0.839** |
| **exp6 blend BGE (rrf_w=0.9)** | **0.950** | **0.925** | 0.817 |

**BGE follow-up** (`BAAI/bge-reranker-base`, rrf_w=0.9, rerank_top_n=30): hits
the ≥0.95 target — fixes **q17** (`APIWebSocketRoute`), only **q03** remains.
MiniLM top_n=60 at rrf_w=0.9 regressed q02/q17 vs MiniLM top_n=30.

**Interpretation:** Reranking is a real lever. MiniLM blend (+0.017 recall,
+0.073 MRR at rrf_w=0.9) does not fix q03/q17. Pure CE **regresses q02**;
blending 90% RRF / 10% CE recovers the hit. **BGE reranker** is the better
model for this stack — promotes websocket routing without dropping OpenAPI hits.

**Residual:** q03 still misses — `APIRoute` final rank 9, `get_request_handler`
in FTS@6 but outside hydrated rerank pool.

**Conclusion:** Exp6 is worth keeping as an **optional** query-time stage.
Default model should be **BGE** when rerank is enabled (`--rerank-model
BAAI/bge-reranker-base`). q03 alone may need exp7 (larger limit) or exp8
(chunk boundaries).

**Artifacts:** `runs/exp6.json`.

---

## Deferred from original plan (not run yet)

These were in the Week 1 improvement sketch but were **not experimented on** —
still valid if q03/q17 reranking doesn't close the gap:

| idea | original rationale | status | notes |
|---|---|---|---|
| Tree-sitter chunk boundaries | chunks too big/small dilute embeddings | **not run** | `parser.py` still extracts whole functions/classes. Large symbols (APIRoute 164 lines) may benefit from splitting or summarizing methods separately. |
| Import-following / multi-hop | auth logic spread across imported files | **not run** | Second retrieval pass following imports of top chunks. Natural after reranker if single-hop hits plateau. |
| FTS weight tuning (exp2) | tune RRF fusion | **run, rejected** | Redundant once exp3+top_n in place; equal weights optimal. |
| Hard exclude vs soft filter for tests | demote vs remove test paths | **partial** | exp5 = hard exclude at retrieval; exp3 = soft post-fusion demotion. Both kept. |

---

## Proposed next experiments (exp6+)

Prioritized by evidence from exp5 residuals. All assume current shipped stack unless
noted.

### Exp 6 — Cross-encoder reranker (DONE — see section above)

**Status:** run; partial. MiniLM blend rrf_w=0.9 preserves hit@5, lifts recall/MRR
but does not close q03/q17; BGE blend rrf_w=0.9 fixes q17, leaving only q03. Not
default in prod.

### Exp 7 — Raise final `limit` / agent context window

**Hypothesis:** relevant chunks are already in fused top 6–9; agent may succeed
with a larger context even if @5 eval misses.

**Approach:** sweep `--limit` 15–20 with `--k` still 5 for strict metrics, or
report hit@10 alongside hit@5.

**Risk:** inflates latency/cost for production agent; measure before shipping.

### Exp 8 — Class-aware embedding splits (chunk boundaries)

**Hypothesis:** `APIRoute` embedding still weak despite NL header; splitting large
classes into per-method chunks (or embedding method signatures as separate rows)
improves vector rank for routing questions.

**Approach:** parser change + **re-ingest required**. A/B under separate
`installation_id` to preserve comparability.

### Exp 9 — Import-following second hop

**Hypothesis:** some questions need symbols from imported modules not co-located
with the first hit.

**Approach:** parse imports from top-k chunks, retrieve defining chunks, merge
into context. Eval may need new golden labels for multi-file questions.

---

## Combination / stacking notes

Measured pairings so far:

- **exp1 × exp3 = strong positive (shipped).** exp1 alone regresses (0.80→0.70)
  because the revived FTS is noisy; exp3 removes the noise at its source
  (tests/tutorials), flipping it to a net win (0.85/0.792/0.782). Neither is
  worth shipping without the other.
- **exp2 ⊂ exp3+top_n (redundant).** FTS weight tuning helped before exp3
  (0.2→0.75) but adds nothing after it (equal weight is optimal). Demotion +
  top_n made the weight knob unnecessary. Lesson: fix signal *quality* before
  tuning fusion *weights*.
- **top_n is a real lever, not just plumbing.** 20→60 added a full hit by
  letting deep-but-correct vector results (rank ~59) reach fusion. Cheap
  (query-time), but watch the q17-style dilution trade-off.
- **exp4 = independent recall lever (shipped).** Orthogonal to exp1/exp3: it
  doesn't change the hit set but lifts recall on multi-relevant questions
  (q01/q05) via the NL header. Stacks cleanly on exp1+3 with no regression.
- **exp5 = the missing piece for q02 (shipped).** Stacks on exp1+3+4. Confirms
  exp4's diagnosis: the blocker was retrieval-time crowding, not embedding
  quality. Filter is query-time (cheap) and orthogonal to exp4's re-embed.
- **exp3 post-fusion demotion + exp5 retrieval filter = complementary.** Exp3
  reorders what was retrieved; exp5 changes *what gets retrieved*. Both kept:
  filter for the candidate pool, demotion for edge paths that slip through.
- **Remaining gap = q03/q17.** q03 has relevant chunks in the pool (vector 20 /
  FTS 6) but not top-5; q17 at final rank 6–7. Exp6 rerank did not close the gap.
  Larger final `limit` (exp7) or class-aware splits (exp8) are the natural next steps.

Current best (shipped) = **exp1 + exp3 + exp4 + exp5** → **0.900 / 0.858 / 0.766**
vs baseline 0.800 / 0.767 / 0.649. Remaining misses: **q03, q17**.

**Lessons for future experiments:**

1. Fix signal quality (FTS, filter noise) before tuning fusion weights.
2. Post-fusion tricks can't fix what never enters `top_n` — filter at retrieval first.
3. Re-ingest is expensive; prefer query-time changes when the diagnosis allows.
4. Record every run with `--label` + `--out`; update this file's leaderboard + matrix.
5. When an experiment fails its hypothesis but diagnoses the real blocker, log that
   explicitly (exp4 → exp5 is the template).
