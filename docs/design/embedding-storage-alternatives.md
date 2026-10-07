# Alternatives to halfvec(1536) — research for storage-capacity-plan phase 1

**Date:** 2026-09-22 · **Status: research complete; option B selected and implemented.**

> **Outcome (2026-09-24):** Exp7 selected `halfvec(1536)` with no ANN index, exp8
> confirmed the quality and latency gates, and the application now ships that
> configuration by default. The option descriptions and estimates below are retained
> as the pre-decision research record.

Deep-dive requested before implementing phase 1 of
[storage-capacity-plan.md](../storage-capacity-plan.md): what else could we do instead
of (or in addition to) `halfvec(1536)`? Three research tracks: Postgres-side options,
embedding-model-side options, and moving vectors out of Postgres. All external numbers
cited; vendor-benchmark claims flagged. Baseline: 23,309 chunks → 370 MB in
`code_chunk_embeddings` (≈16 KB/chunk: ~6.3 KB vector data + ~8 KB HNSW + heap
overhead), quality gate hit@5 0.900 / recall@5 0.858 / MRR 0.766 ±jitter.

## Two structural facts the alternatives exploit

1. **Every query is per-repo, but the index is global.** `_vector_search`
   (`Backend/app/services/search.py`) joins to `code_chunks` and filters by
   repo/ref/generation/path, while `ix_embeddings_hnsw` (`Backend/app/main.py`) is one
   HNSW over all repos (m=16, ef_construction=64). Per-repo cardinality is 5k–30k
   vectors — well under the ~100k–1M threshold where the brute-force-vs-ANN literature
   says ANN starts paying for itself. Also note `top_n=60` exceeds pgvector's default
   `hnsw.ef_search=40`: an HNSW scan yields at most ~40 candidates before the join
   filters run. Either the planner is skipping the index for these filtered queries
   (we pay the 8:1 index storage for nothing) or the vector retriever is candidate-
   starved. **Worth an `EXPLAIN ANALYZE` before any migration.**
2. **`code_chunks` itself costs ~2 KB/chunk** (47 MB / 23.3k). Once embeddings drop
   below that, further embedding compression stops moving the total. In-Postgres,
   anything past ~4× compression has diminishing returns.

## Option space

### A. halfvec(1536) + halfvec HNSW — the plan as written

~165 MB all-in (**2.2×**, ~7 KB/chunk). Data row 3,080 B (still TOASTs; `SET STORAGE
PLAIN` keeps it inline), index ~4 KB/vector (measured: fp32 7.7 GB → fp16 3.9 GB on
dbpedia-openai-1M). Best-evidenced quality story of any option: recall **96.8% for
both fp32 and halfvec** at identical settings on 1536-d OpenAI vectors, halfvec
slightly faster ([Katz, scalar & binary quantization benchmarks](https://jkatz05.com/post/postgres/pgvector-scalar-binary-quantization/)).
Supported everywhere we care about: Neon ships pgvector 0.8.0 (PG14–17), RDS ships
0.8.2. Effort: `ALTER TABLE … TYPE halfvec` + reindex + update the two `CAST(… AS
vector)` sites. **Low risk, modest win. Verdict: still the right first step.**

### B. Drop the ANN index entirely — exact per-repo scan (sleeper pick)

halfvec column, **no vector index**, btree-driven filter on repo, `SET STORAGE PLAIN`.
~72 MB all-in (**5×**, ~3.1 KB/chunk) — the entire 8:1 index overhead disappears.
Recall is exactly 1.0, so the eval gate is trivially satisfied (can only match or
beat baseline). Cost is latency: measured ~40 ms brute-force over 10k×1536-d
([mydba.dev](https://mydba.dev/blog/pgvector-missing-index)); extrapolated **~50–250 ms
per query for 5k–30k rows on t4g-class CPUs** (not measured on Graviton2 — needs our
own timing). QPS is low for an onboarding product, and halfvec halves both I/O and
arithmetic. Revisit if a repo exceeds ~50k chunks. Per-repo *partial* HNSW indexes are
the middle ground but need DDL per repo and planner-matching footguns — skip.

### C. Binary quantization + rerank — the escalation path

Keep halfvec column, add expression index `USING hnsw
((binary_quantize(embedding)::bit(1536)) bit_hamming_ops)`, two-stage query (hamming
top-~200 → halfvec cosine rerank). ~83 MB all-in (**4.5×**), likely *faster* than
today (1.34–1.42× QPS measured). Quality: raw BQ **fails** the gate (66.8% recall);
with full-precision rerank 91.6% at ef_search=40 up to 99.0% at 200 (all on
ada-002-1536, the closest public proxy — no published text-embedding-3-small number;
Qdrant corroborates 0.98 recall at 4× oversampling for 1536-d OpenAI). Medium effort:
two-stage SQL. Only worth it if A+B prove insufficient — and note the ~2 KB/chunk
`code_chunks` floor caps what it can win.

### D. Fewer dimensions at the source — Matryoshka truncation

`dimensions=512` on the existing `text-embedding-3-small` call (or 768 per the plan's
current option): 512+halfvec ≈ 1 KB/vector (**~6× on embeddings**). OpenAI's published
MTEB drop at 512 is small (62.3 → 61.6; per-dim rows not independently re-verifiable —
blog 403s), but no code-retrieval numbers exist; our eval is the only gate.
**Zero-friction to evaluate: client-side truncate + L2-renormalize of already-stored
vectors is equivalent to the API parameter** (Supabase verified empirically), so the
experiment needs no re-embedding — pull vectors, truncate, score offline.
`text-embedding-3-large@256/512` (6.5× the price, still ~$1.63/repo) is the
same-vendor fallback if 3-small truncates poorly; 3-large@256 > ada@1536 is published.

### E. Code-specialized model — voyage-code-4 (quality play, not a storage play)

voyage-code-4 (Aug 2026): dims 2048/1024/512/256, **quantization-aware float/int8/
binary output**, $0.12/1M with the **first 200M tokens free** (≈ our first ~16 repos).
Vendor benchmarks: +40% over OpenAI-3-large on classic code retrieval; predecessor
code-3's binary@256 reportedly beats 3-large-float@3072. Sensible pgvector pairing:
`bit(1024)` + hamming HNSW (128 B/vector) with optional halfvec rescore column.
Realistically this is the only option that might *raise* hit@5 while shrinking
storage — but it's a new vendor (MongoDB-owned), all quality deltas are vendor-run,
and pgvector has no int8 type so int8 output is useless to us (fp16 or binary only).
Worth a gated experiment when retrieval quality (not storage) becomes the priority.

### F. Vectors out of Postgres — S3 fp16 shards + in-process brute force

Per-repo `.npy` fp16 shard on S3 (30k chunks ≈ 92 MB), downloaded to EBS on first
query, `np.memmap` + matmul + argpartition. Warm query 5–15 ms (exact, recall 1.0);
cold ~1 s. Page-cache-backed memmap sidesteps the 490 MiB worker RSS cap and is
shared across all four processes. FTS stays in Postgres; RRF fusion already happens
in app code (`_rrf_fuse`), so hybrid barely changes — the vector retriever becomes a
local matmul instead of a SQL query. **This is the option that changes phase 2, not
phase 1: Postgres shrinks to the <0.5 MB relational core + `code_chunks`, Neon free
suffices indefinitely, and the $14/mo RDS migration loses its rationale.** Cost
~$0.15/mo at 5 GB of vectors. Price: 1–2 days of code, shard invalidation on
re-ingest (staleness semantics acceptable for a cache), and losing single-statement
SQL locality. Runner-up embedded engines if ANN is ever needed: LanceDB on EBS
(<30 ms p95). Disqualified: DuckDB VSS (HNSW must be RAM-resident, unbounded),
self-hosted Qdrant (needs 500 MB–1.2 GB we don't have), Upstash (10k-vector free cap),
Qdrant Cloud free (suspends after 1 idle week, deleted after 4), Turbopuffer ($16/mo
floor > RDS anchor), pgvectorscale (not on RDS **or** Neon — dead end).

Split-across-free-tiers hack (2nd Neon project for vectors): within ToS, but caps at
~2× today with halfvec and adds cold starts — buys months, not a solution. Skip.

## Comparison (23.3k chunks; "all-in" = vector data + vector index)

| Option | All-in | Per chunk | Quality risk | Effort | Neon/RDS |
|---|---|---|---|---|---|
| Historical baseline: fp32 + HNSW | 370 MB | 15.9 KB | — | — | ✅/✅ |
| **A. halfvec + HNSW (plan)** | ~165 MB | 7 KB | ~none (published parity) | Low | ✅/✅ |
| **B. halfvec, no index (exact scan)** | ~72 MB | 3.1 KB | none (recall 1.0); latency gate later passed | Low | ✅/✅ |
| C. halfvec + BQ expr. index + rerank | ~83 MB | 3.6 KB | needs eval (proxy: 92–99% w/ rerank) | Medium | ✅/✅ |
| D. 3-small@512 + halfvec (+B) | ~40 MB | ~1.7 KB | needs eval; testable offline free | Low | ✅/✅ |
| E. voyage-code-4@1024 bit + rescore | ~55 MB | ~2.4 KB | needs eval; plausibly *better* | Medium + vendor | ✅/✅ |
| F. S3 fp16 shards + numpy | ~0 in PG | ~$0.03/mo/GB | none (exact) | 1–2 days | n/a (kills RDS need) |

## Original recommendation and outcome

1. **Proceed with phase 1 halfvec — the research validates it.** Best
   evidence-to-risk ratio, and every other option composes with it rather than
   replacing it.
2. **Add one cheap diagnostic and one extra variant to the same eval run:**
   - `EXPLAIN ANALYZE` the production-shaped vector query first. If the planner
     already ignores the global HNSW for repo-filtered queries, option B (drop the
     index) is free storage and the eval baseline already reflects exact-scan
     quality. Measure per-query latency at 30k chunks either way; if it clears the
     latency budget, **halfvec + no ANN index (B) beats the plan as written** — 5×
     instead of 2.2×, with a *guaranteed* eval pass.
   - The 768-dim lever already in the plan can be evaluated **without re-ingesting**:
     truncate + renormalize stored 1536-d vectors offline (equivalent to the API
     `dimensions` param). Score 512 while at it.
3. **Re-decide phase 2 after phase 1 lands.** This was the original next step. Option B
   passed; 512 dimensions failed the follow-up quality gates; Neon was retired; and the
   current phase-2 target remains a fresh RDS database. Option F stays a future
   alternative if exact-scan heap residency becomes the limiting cost.
4. **Park voyage-code-4** as the quality-upgrade experiment for when the retrieval
   log unpauses — gated on our eval, not vendor benchmarks.

**Follow-up resolution:** exact-scan latency was directly measured in exp8-C at
16.09 ms p95 for the largest 11,742-chunk ref, comfortably inside the 250 ms gate.
Still unverified: Graviton2-specific performance; all Voyage cross-vendor deltas
(vendor-run); BQ proxy numbers; and OpenAI per-dimension MTEB rows.

Key sources: [Katz — pgvector quantization benchmarks](https://jkatz05.com/post/postgres/pgvector-scalar-binary-quantization/) ·
[pgvector README](https://github.com/pgvector/pgvector) ·
[RDS extension versions](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html) ·
[Neon extensions](https://neon.com/docs/extensions/pg-extensions) ·
[Supabase Matryoshka](https://supabase.com/blog/matryoshka-embeddings) ·
[Qdrant binary quantization](https://qdrant.tech/articles/binary-quantization/) ·
[voyage-code-4](https://blog.voyageai.com/2026/08/13/voyage-code-4/) ·
[Voyage pricing](https://docs.voyageai.com/docs/pricing) ·
[Turnbull — just brute force your embeddings](https://softwaredoug.com/blog/2026/07/29/just-brute-force-embeddings) ·
[Rosenthal — do you need a vector DB?](https://www.ethanrosenthal.com/2023/04/10/nn-vs-ann/) ·
[mydba — exact scan timing](https://mydba.dev/blog/pgvector-missing-index) ·
[pgvector#980 — filtered HNSW](https://github.com/pgvector/pgvector/issues/980) ·
[AWS — pgvector 0.8.0 iterative scans](https://aws.amazon.com/blogs/database/supercharging-vector-search-performance-and-relevance-with-pgvector-0-8-0-on-amazon-aurora-postgresql/)
