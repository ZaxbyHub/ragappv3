# Model qualification — E05 ablation grid — 2026-09 (issue #36)

Ablations on the real deployment (R640AI, deployed commit 281bd714,
harrier-oss-v1-0.6b + bge-reranker-v2-m3 via TEI 1.9.3) against the frozen
63-query calibration dataset (backend/tests/eval/calibration_2026_09/).
Decision rule pre-declared in the approved plan: a candidate wins only with
≥ +0.05 absolute gold-recall@7 on the labelled set; anything less — or a
failed attempt — is a documented negative outcome, never parity. Raw
per-query data: raw-data: 2026-09-model-qualification-data.json

## Harrier model-card verification

Verified against the primary model card (see 2026-09-model-research.md for
sources and access dates): documents take NO prompt (deployed index is
correct), queries REQUIRE the E5-style instruction — and the deployment sends
none (`EMBEDDING_QUERY_PREFIX` unset). The prefix-only A/B re-embedded the 55
content queries with the card instruction against the same frozen index:
gold-recall@7 0.927 → 0.964 (delta +0.036), rank-1-gold 32/55 → 38/55, MRR
0.730 → 0.799. Consistently positive but below the pre-declared bar → no
default change in this PR; recommended as a controlled deployment trial. No
re-embed is required to trial it (query-side only, document index unchanged).

## Reranker A/B (frozen pool)

Identical top-20 candidate pools per query (from the frozen score dump);
baseline = deployed bge-reranker-v2-m3 (TEI, sigmoid contract); challenger =
Qwen3-Reranker-0.6B served via llama.cpp `--rerank` (GPU 0; Q8_0 GGUF;
comparability=ad-hoc-gpu — different runtime, ranking-only comparison).
Result: baseline gold-recall@7 0.979 (47/48) vs challenger 0.896 (43/48);
delta −0.083 — beyond the noise floor in the BASELINE's favor. 7/55
challenger requests returned HTTP 500 (long-document handling; retained as
failures, not retried). Verdict: challenger REJECTED; the deployed reranker
stands. The frontier-audit's external ranking claims for Qwen3-Reranker did
not reproduce on this corpus.

## Contextual chunking ablation

Scoped-subset A/B on an isolated instance pair (identical image, env, TEI
endpoints; own data volumes): 3 CDP documents (399 chunks), 6 labelled
queries derived from those files, gold-recall@7 of the full
retrieve→rerank→top-7 path:

- contextual_chunking_enabled = false (deployed default): 6/6 = 1.000
- contextual_chunking_enabled = true (one instant-LLM context call per chunk
  during re-ingest): 6/6 = 1.000 (399 contextual chunks; ingestion took ~90
  minutes against ~3 minutes for the OFF arm on identical hardware).

Verdict: NEUTRAL/NEGATIVE at this scope — both arms saturate the subset's
gold recall, so contextual chunking produced no measurable retrieval gain
here while adding ~30× ingestion wall-time (one LLM call per chunk). The
deployment default (off) is retained; the subset was deliberately small to
bound the per-chunk cost, and a corpus-wide trial is only justified if a
future, harder labelled set shows the OFF arm failing. Subset limits
disclosed: single-instance pair, 3 files, 6 queries, same-day run.

## Top-k / top-n grid

initial_top_k ∈ {10, 20} × rerank_top_n ∈ {4, 7, 10}, gold-recall@7 over the
55 content queries: k10 → 0.909–0.927; k20 → 0.927–0.945. The deployed
default (top_k 20 → top_n 7, 0.945) ties the grid optimum; widening top_n to
10 adds nothing (0.945) and shrinking the initial pool to 10 costs recall.
Verdict: keep deployed retrieval sizing; no change.

## Embedding challenger (attempted — negative serving outcome, retained)

Qwen3-Embedding-0.6B (frontier-audit lead, claimed MTEB v2 ≈ 70.5 vs
Harrier 69.0; 1024-d — index-compatible without a dimension rebuild):
TEI-servability was CONFIRMED (a TEI 1.9.3 container initialized the model
on the FlashQwen3 backend, GPU 0), and an isolated challenger app instance
was provisioned with its own versioned index (own data volume,
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B pointed at the challenger TEI).
Two full serving attempts were made: attempt 1 loaded the model but sat on
the wrong docker network (removed and recreated); attempt 2 stalled in the
1.2 GB weights download for >15 minutes inside the session's execution
budget. The ingestion arm therefore recorded 100% embedding failures
(213/213, 140/140, 46/46 chunks — failures retained verbatim in the trace)
and the comparison could not be completed. Disposition: OPEN negative
outcome — the arm is reproducible with the recorded procedure (exact docker
run lines in the trace scratch) and remains follow-up work; no adoption
claim is made in either direction. The frontier-audit's ranking numbers for
this model remain unverified experiment leads.

## Negative results

Retained deliberately (the issue requires keeping failures and negative
outcomes):

1. Qwen3-Reranker-0.6B rejected (−0.083 recall vs baseline; 7 HTTP-500
   failures on long documents retained in the data JSON).
2. Harrier query prefix: positive direction but below the pre-declared bar
   (+0.036 recall) — not adopted; recorded as a deployment trial
   recommendation.
3. Top-k/top-n widening: no gain at the deployed optimum.
4. Contextual chunking: no measurable gain on the scoped subset at ~30×
   ingestion cost (both arms 6/6) — default stays off.
5. Qwen3-Embedding-0.6B challenger: serving attempt not completed within
   the session budget (see above) — open, retained.
6. Derived rerank band candidates (0.957/0.473/0.181 and 0.974/0.715/0.29)
   rejected by held-out validation (84.1% / 77.9% vs shipped 85.0%) — see
   calibration analysis.
7. First ON-arm serving attempt of the reranker challenger (unsloth GGUF
   repo) failed to load in llama.cpp; replaced by mradermacher Q8_0
   (attempt retained in the trace log, not in this doc's data).

## Per-case raw results

The data JSON carries per-query rows for the reranker A/B (challenger
rankings and errors per query) and the qualification doc's arms derive from
per-query raws retained in the trace scratch (e05_ablation_raw.json:
per-query prefixed rankings and grid results; rerank_ab_raw.json: per-query
challenger rankings incl. the 7 HTTP-500 failures; ablation_score_off/on
.json: per-query chunking outcomes). Summary fields in the data JSON are
computed from those per-case rows by scripts/build pass, never hand-entered.
