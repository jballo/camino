# Exp8 plan — make the dimension-truncation result conclusive

**Date:** 2026-09-23 · **Status: COMPLETE. Stages A–C done — every 512 gate failed; final answer is `halfvec(1536)`. Stage C latency gate passed ~15× under (16.09 ms p95 at 11,742 chunks). Stage D/exp9 will not run.** Follow-up to
[phase1-halfvec-experiment-plan.md](phase1-halfvec-experiment-plan.md) (exp7).
Does **not** touch the phase-1 decision: V2 (`halfvec(1536)`, no ANN index) is
selected and its cutover proceeds independently.

## Conclusion (2026-09-23)

Exp8 answered both questions exp7 left open, and the answer is final:
**Camino stays on `halfvec(1536)` with no ANN index.** No further stages
remain — Stage D was conditional on Stage B passing and correctly does not
run; exp9 (512 migration) is not opened.

1. **Dimension truncation is settled, adversely.** The exp7 "512 looked
   better" result was a small-sample fusion artifact. On 58 pooled questions
   across three dissimilar corpora (FastAPI/Python, deepeval/Python monorepo,
   firecrawl/TypeScript monorepo), truncating to 512 measurably degrades the
   vector retriever itself (pooled vector-only ΔMRR@5 −0.037, 95% CI
   [−0.078, −0.004] — excludes zero), and hybrid fusion merely masks that
   loss (Δ@5 −0.000, but CI too wide to certify "no harm"). Every adoption
   gate failed: deepeval lost hit@5 with a newly missed question, the pooled
   CI could not exclude worse-than-−0.02, and the degradation is
   corpus-dependent — the exact property that makes truncation unsafe to ship
   blind. 768 never dominated 512 and is equally dead. The ~93 MB storage
   payoff is forgone knowingly; revisit only with real-query eval data from
   the retrieval log.
2. **V2 latency is measured, not extrapolated, and passes with ~15×
   headroom.** Direct 50-iteration production-shaped scans over eight live
   refs (1.6k–11.7k chunks, 51,059 total) put the largest single ref at
   **16.09 ms p95** against the 250 ms gate, scaling linearly at ~0.7–2.3
   µs/chunk — a 25–30k-chunk repo projects under ~70 ms p95. The V2 cutover
   has no remaining latency caveat.

Full data: `EXP 8-A/8-B/8-C` in `Backend/eval/EXPERIMENTS.md`; measured
latency table also in `docs/storage-capacity-plan.md` phase 1.

Secondary finding worth its own follow-up (out of exp8 scope): absolute
retrieval quality on the two new corpora is far below FastAPI at every
dimension (hybrid hit@5 0.44–0.50 vs 0.90, all labels indexed). The
FastAPI-tuned fusion/limit settings generalize poorly to large monorepos —
candidate work alongside exp6 (reranking) and exp9-as-limit-sweep.

## What exp7 left open

1. **The 768/512 result is non-monotonic and under-powered.** On 20 questions,
   one rank shift flips hit@5 by 0.05. V3@768 "regressed" because q02's answer
   moved from final rank 5 to 6 — and the raw run JSON shows the vector
   retriever actually *improved* at 768 (`get_openapi` vector_rank 4 vs 5 at
   1536); the miss came from RRF fusion with a weak FTS rank (14). So the
   headline regression is a **fusion/cutoff artifact**, not evidence that 768
   dims lose semantic quality. Conversely V3@512's improvement (recall 0.875,
   MRR 0.793) is a couple of rank shifts on the same 20 questions. Neither
   number should drive a migration.
2. **V2's latency at 25–30k chunks is extrapolated (×4–6), not measured.** The
   local testdb can hold the t4g 5-repo corpus (~23k chunks), so this can be
   measured directly before cutover instead of waiting for production telemetry.

**Decision rule:** 512 becomes a migration candidate (exp9, the §7 pure-SQL
path with 512 substituted for 768) only if **all Stage B gates** pass. Anything
short of that → stay at 1536 and revisit when the retrieval log provides
real-query eval data. 768 is out of the running unless the Stage A sweep shows
it strictly dominating 512 (it currently loses on every aggregate metric).

Payoff if 512 passes: `halfvec(512)` is 1,024 B/vector vs 3,072 B — the 140 MB
V2 footprint drops to ~47 MB, ≈12× total capacity vs the original fp32+HNSW.

## Continue here — execution handoff (2026-09-23)

### Completed

- Stage A harness is implemented: `eval.run_eval --dataset PATH` and
  `eval.analyze_dims` (rank trajectories, k=5/k=10 metrics, and deterministic
  10k paired-bootstrap CIs). Both tools reject an all-unindexed fixture instead
  of producing a misleading zero result.
- The valid FastAPI sweep and jitter runs are in gitignored
  `eval/runs/exp8_sweep_{h,v}_*.json` and `exp8_jitter_{1,2}.json`.
- Stage A is logged as `EXP 8-A` in `eval/EXPERIMENTS.md`. The conclusion is
  adverse for 512: hybrid MRR improves to 0.793, but vector-only MRR falls by
  0.096 vs 1536 with paired 95% CI [−0.192, −0.017]. Hybrid jitter is only
  0.001 MRR. Stay at `halfvec(1536)`; no migration is approved.
- q02's original 768 miss is confirmed as fusion/cutoff behavior: raw vector
  rank remains 4 while hybrid final rank moves 5→6. Separately, the vector-only
  curve is flat only through roughly 1024 and degrades across several questions
  from 896 downward. 768 does not strictly dominate 512 and remains out.
- Stage B golden sets were user-approved:
  - `eval/golden_dataset_deepeval.json`: 18 questions, tag
    `python-v4.2.4`, commit `da9b2c969f8a15e8aa4ef67c372157ce060cd63e`.
  - `eval/golden_dataset_firecrawl.json`: 20 questions, tag `v2.11.0`,
    commit `ef12eb36b2f3382838dfe0a0c1a5add3d5df7fe5`.
  Every labeled file/symbol was statically confirmed in the pinned source.
  First-run `indexed: false` diagnostics still take precedence and must be fixed
  as parser mismatches rather than scored as retrieval failures.
- Pinned shallow clones exist at gitignored `eval/.data/deepeval` and
  `eval/.data/firecrawl`.
- Stage C code is ready: `eval.explain_vector` accepts either `--dataset PATH`
  or `--repo NAME --ref REF --query TEXT`. No Stage C measurements have run.

### Safety rule learned during Stage A

Never use bare `doppler run -- ...` for local eval work: Doppler replaces
`DATABASE_URL` with its configured remote value. Always use the wrapper below,
which applies the local throwaway override *after* Doppler:

```bash
cd Backend
export EXP7_DATABASE_URL=postgresql://loadtest:loadtest@localhost:5433/loadtest
exp7() {
  doppler run -- env DATABASE_URL="$EXP7_DATABASE_URL" "$@"
}
```

The invalid all-zero Stage A artifacts caused by bare Doppler were overwritten
with valid local-testdb runs. `eval.run_eval` is read-only, and its new preflight
now aborts before query embeddings when no golden labels exist at the target.

### Stage B — completed 2026-09-23 (Claude-run under scoped user authorization)

The user lifted the "only the user runs evals" rule for Stage B only. Results
are logged as `EXP 8-B` in `eval/EXPERIMENTS.md`: both pinned-tag ingests
succeeded, both 1536 hybrid validations had zero unindexed labels (no label
fixes needed), the full 5-dim × 2-mode matrix ran on both new corpora, and
`eval.analyze_dims` gained the pooled per-question analysis (question IDs
namespaced by `repo@ref`; dims pooled only when present in every corpus;
`sweep`-labeled baselines preferred when duplicates exist).

**Outcome: 512 rejected — all three gates failed.** Pooled hybrid delta@5 is
−0.000 but its CI [−0.034, +0.029] doesn't exclude −0.02; deepeval hybrid@512
loses hit@5 (0.444→0.389, q17 newly missed at rank 5→6); pooled vector-only
512 replicates Stage A at −0.037 MRR@5, CI [−0.078, −0.004]. Final answer:
stay at `halfvec(1536)`. Stage D and exp9 do not run.

Credential note: Bash commands may not contain `user:password@host` URLs (the
post-incident guardrail blocks them). Use `Backend/eval/exp7_run.sh`, which
holds the documented throwaway testdb URL and applies it after Doppler.

The original run plan follows for the record.

#### Original run plan (executed above)

The testdb currently has moving `main` refs for both repos; the approved golden
sets use pinned tags, so ingest those tags as separate refs:

```bash
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none \
  uv run python -m eval.ingest_local \
  --path eval/.data/deepeval --repo confident-ai/deepeval \
  --ref python-v4.2.4 --no-clone

exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none \
  uv run python -m eval.ingest_local \
  --path eval/.data/firecrawl --repo firecrawl/firecrawl \
  --ref v2.11.0 --no-clone
```

Run the 1536 hybrid validation first, inspect every `NOT INDEXED` diagnostic,
and correct labels before starting the matrix:

```bash
exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none \
  uv run python -m eval.run_eval \
  --dataset eval/golden_dataset_deepeval.json \
  --vector-dims 1536 --mode hybrid \
  --label exp8_b_deepeval_h_1536 \
  --out eval/runs/exp8_b_deepeval_h_1536.json

exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none \
  uv run python -m eval.run_eval \
  --dataset eval/golden_dataset_firecrawl.json \
  --vector-dims 1536 --mode hybrid \
  --label exp8_b_firecrawl_h_1536 \
  --out eval/runs/exp8_b_firecrawl_h_1536.json
```

After both validations have zero `NOT INDEXED` labels, run dimensions
`{1536, 768, 640, 512, 384}` in hybrid and vector modes. 640/384 are included
because Stage A found the neighborhood around 512 unstable:

```bash
for d in 1536 768 640 512 384; do
  for mode in hybrid vector; do
    short=${mode:0:1}
    exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none \
      uv run python -m eval.run_eval \
      --dataset eval/golden_dataset_deepeval.json \
      --vector-dims "$d" --mode "$mode" \
      --label "exp8_b_deepeval_${short}_${d}" \
      --out "eval/runs/exp8_b_deepeval_${short}_${d}.json"
  done
done

for d in 1536 768 640 512 384; do
  for mode in hybrid vector; do
    short=${mode:0:1}
    exp7 VECTOR_TYPE=halfvec VECTOR_INDEX=none \
      uv run python -m eval.run_eval \
      --dataset eval/golden_dataset_firecrawl.json \
      --vector-dims "$d" --mode "$mode" \
      --label "exp8_b_firecrawl_${short}_${d}" \
      --out "eval/runs/exp8_b_firecrawl_${short}_${d}.json"
  done
done

uv run python -m eval.analyze_dims \
  eval/runs/exp8_b_deepeval_*.json \
  eval/runs/exp8_b_firecrawl_*.json
```

`eval.analyze_dims` currently reports each repo/ref separately. Before the final
Stage B decision, extend it to pool paired per-question rows across FastAPI,
Deepeval, and Firecrawl (namespace IDs by repo so each repo's `q01` remains
distinct), then report the pooled 512−1536 MRR delta and bootstrap CI. Do not
declare the gate from an unweighted average of per-repo aggregate metrics.

Apply every Stage B gate already defined below. Given the adverse Stage A vector
result, 512 still passes only if **all** gates hold. Anything short means the
final answer is `halfvec(1536)` and Stage D does not run.

### Stage C — completed 2026-09-23 (Claude-run, authorization extended by user)

Logged as `EXP 8-C` in `eval/EXPERIMENTS.md`. With the Stage B tag ingests the
testdb held eight live refs / 51,059 chunks (1.6k–11.7k per ref), so no
further ingests were needed. 50-iteration production-shaped exact scans per
ref: the largest ref, `confident-ai/deepeval@main` (11,742 chunks), measured
**16.09 ms p95** — ~15× under the 250 ms gate — and scaling across the eight
sizes is roughly linear at ~0.7–2.3 µs/chunk. `storage-capacity-plan.md`
phase 1 now carries the measured table in place of the ×4–6 extrapolation.

Environment correction discovered during Stage C: the database on
`localhost:5433` is Compose service **`camino-testdb-1`**; the
`camino-exp7-testdb` container named below has no host port mapping and was
never the eval target. Results unaffected.

### Stage D state

Will not run: Stage B gates failed (see `EXP 8-B`), so the truncation ≡
native-512 check is moot and exp9 is not opened.

## Ground rules (unchanged from exp7)

- **Only the user runs evals/tests.** Code + exact commands are prepared; the
  user executes and reports output.
- All runs against the **local testdb** (`camino-testdb-1`, the Compose
  `testdb` service on `localhost:5433` — restart with
  `docker start camino-testdb-1`; it holds the V2-schema corpus). Never Neon.
- Never print `.env` contents or env secrets. Testdb creds
  (`loadtest:loadtest@localhost:5433/loadtest`) are documented throwaways.

## Stage A — squeeze the existing corpus (no new labels, no re-embed, ~$0)

Goal: characterize the dim–quality curve finely enough to tell **spike (noise)
from trend (signal)**, and separate vector-retriever quality from fusion
artifacts.

1. **Code:** small PR —
   - `eval/run_eval.py`: add `--dataset PATH` (default unchanged:
     `golden_dataset.json`). Needed for Stage B; harmless now.
   - New `eval/analyze_dims.py`: reads a set of `runs/*.json`, and prints
     (a) per-question `first_rank` trajectory across dims (the q02-style
     movements at a glance), (b) hit/recall/MRR at k=5 **and k=10**,
     (c) a paired bootstrap (resample questions, 10k iterations) CI for the
     MRR delta of each dim vs 1536. The existing run JSONs already carry
     everything needed (`per_question[].diagnosis[].vector_rank/final_rank`).
2. **Runs** (user, testdb on current V2 schema): dimension sweep
   `dims ∈ {1536, 1280, 1024, 896, 768, 640, 512, 384, 256}` × modes
   `{hybrid, vector}`:

   ```bash
   for d in 1536 1280 1024 896 768 640 512 384 256; do
     uv run python -m eval.run_eval --vector-dims $d --mode hybrid \
       --label exp8_sweep_h_$d --out eval/runs/exp8_sweep_h_$d.json
     uv run python -m eval.run_eval --vector-dims $d --mode vector \
       --label exp8_sweep_v_$d --out eval/runs/exp8_sweep_v_$d.json
   done
   uv run python -m eval.analyze_dims eval/runs/exp8_sweep_*.json
   ```

   The `--mode vector` runs are the important half: truncation only touches the
   vector retriever, and hybrid RRF + path-penalty both dampen and (as q02
   showed) occasionally invert its effect.
3. **Noise floor:** rerun the 1536 hybrid baseline twice more
   (`exp8_jitter_{1,2}`). Query embeddings come from OpenAI per run, so this
   measures run-to-run jitter — the empirical error bar the ±0.01 MRR gate is
   judged against.
4. **Read-out:** if 768's dip is an isolated spike while 640/896 track 1536,
   it's confirmed cutoff noise; if the vector-only curve is flat down to ~512
   and degrades below, that's the expected Matryoshka shape and 512 stays a
   candidate. Log as **exp8-A** in `EXPERIMENTS.md`.

## Stage B — broaden the eval set (the actually-conclusive part)

Goal: enough independent questions across dissimilar repos that "no new
misses" and ±0.01 MRR mean something. Target ~60 pooled questions across 3
repos.

1. **New golden sets, 2 repos** from the t4g matrix (already known to ingest
   cleanly), pinned tags, deliberately unlike FastAPI and each other:
   - `confident-ai/deepeval` — Python, ML-eval domain.
   - `firecrawl/firecrawl` — TypeScript monorepo (also exercises the parser's
     non-Python path in eval for the first time).
   15–20 questions each, same schema as `golden_dataset.json`
   (`eval/golden_dataset_deepeval.json`, `eval/golden_dataset_firecrawl.json`).
   Process mirrors the FastAPI set: Claude drafts questions + labeled
   file/symbol pairs from reading the repos; **the user reviews before any run
   treats them as ground truth.** After first run, check the parser-mismatch
   diagnostics (`indexed: false`) to catch labels the chunker can't represent —
   fix labels, don't paper over.
2. **Ingest** (user): local clones at the pinned tags, then per repo
   `uv run python -m eval.ingest_local --path <clone> --repo <owner/name> --ref <tag> --no-clone`
   into the testdb (V2 schema already in place; new rows inherit it).
3. **Runs** (user, per repo): dims `{1536, 768, 512}` × `{hybrid, vector}`,
   plus any neighbor dims Stage A flagged as unstable, via
   `--dataset eval/golden_dataset_<repo>.json`.
4. **Gates for 512 adoption candidacy** (all must hold):
   - **Per repo:** no newly missed questions vs that repo's own 1536 baseline,
     and hit@5 not below it.
   - **Pooled (~60 q):** MRR(512) − MRR(1536) ≥ −0.01, and the bootstrap CI of
     the delta excludes anything worse than −0.02.
   - **Stability:** the Stage A sweep shows no cliff adjacent to 512 (384 and
     640 within the same envelope), and the deltas exceed the Stage A jitter
     floor before being called improvements.
   Log as **exp8-B**; if passed, open exp9 (migration) as a separate plan.

## Stage C — direct large-corpus latency (closes the V2 extrapolation caveat)

Piggybacks on Stage B's ingests. Add the remaining t4g repos
(`jballo/camino`, `jballo/nous-core`, `timlrx/tailwind-nextjs-starter-blog`)
so the testdb reaches the full ~23k-chunk corpus (~$0.25/repo in embeddings if
not already present). Then:

- `eval/explain_vector.py` against the **largest single repo** (searches are
  repo-filtered, so the single-repo chunk count is what the 250 ms p95 gate is
  about). If `explain_vector.py` is currently pinned to the fixture repo, the
  Stage A PR gives it `--dataset`/`--repo` to match.
- Also record p50/p95 for each repo in the corpus — five sizes give a measured
  O(n) scaling line, replacing the ×4–6 guess entirely. Judge against the
  ≤250 ms p95 gate; update `storage-capacity-plan.md` phase 1 with "measured at
  N chunks" replacing the extrapolation caveat.

## Stage D — only if Stage B passes: truncation ≡ native-512 check (pre-exp9)

The §7 migration keeps old vectors as `subvector(...)` truncations while new
embeds would come from the API with `dimensions=512`. Confirm equivalence
empirically before mixing them: re-embed the FastAPI fixture with
`dimensions=512` (cents), rerun the eval, and diff against the Stage A
512-truncation run — metrics and per-question ranks should match to jitter.
Any real gap → renormalization or API-side behavior needs investigation before
migration. Log as exp8-D; exp9 (the ALTER + `EMBED_DIMENSIONS` change) is its
own plan/PR after this passes.

## Effort / cost summary

| Stage | User time | $ | Blocking? |
|---|---|---|---|
| A sweep + jitter | ~30 min of runs | ~$0 (query embeds only) | no |
| B golden sets | review 2×15–20 drafted questions (~1 h) + runs | ~$0.50 ingest | the conclusive one |
| C latency | one ingest wait + explain runs | ~$0.75 remaining repos | no (closes a caveat) |
| D equivalence | one re-embed + run | cents | only if B passes |

Stage A can run today with zero new code except `analyze_dims.py`; A and the
Stage B labeling can proceed in parallel.
