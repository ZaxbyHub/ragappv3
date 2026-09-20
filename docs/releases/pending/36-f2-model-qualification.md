# 36 — Workstream F PR 2/3: model qualification, relevance calibration, twelve-user performance

Issue: #36 (F2 slot; closes the legacy remaining-02..07 obligations, E05, the
hardware/performance qualification, and MODEL-RESEARCH-01). Trace:
`.agents/issue-traces/36-qualify-models-relevance-performance/` (local).

## What changed

- `frontend/src/lib/relevance.ts` — distance bands recalibrated from the
  frozen 2026-09 deployment calibration (Highly Relevant ≤ 0.56 / Relevant ≤
  0.67 / Related ≤ 0.77; held-out agreement 74.4% vs 30.0% for the old
  0.15/0.3/0.5 bands); rerank bands kept at 0.7/0.4/0.2 with recorded
  provenance (tied-best on held-out, 85.0% vs 84.1% best-derived); the dead
  `rrf` branch removed — the chat source channel only ever emits
  "distance"/"rerank" (the memory channel's rrf/fts/dense vocabulary is
  separate and renders unlabeled). `Source.score_type` narrowed to the
  verified producer contract; unknown/missing scoreType keeps distance
  semantics.
- `backend/app/services/document_retrieval.py` — NEW `FALLBACK_SCORE_FLOOR = 0.5`
  constant decoupling the similarity-score fallback branch (records with no
  `_distance`, higher-is-better) from the calibrated distance max; regression
  coverage in `backend/tests/test_relevance_cutoff_calibration.py` (including
  the dual-mode legacy `relevance_threshold` coupling exercised by the
  adversarial suite). Fixes the CI round-4 regression where raising the shared
  constant silently dropped 0.5–0.75 scores in that branch.
- `backend/app/config.py` — `max_distance_threshold` default 0.5 → 0.75,
  calibrated for the deployed harrier-oss-v1-0.6b cosine scale (measured:
  gold-recall@7 on rerank-off paths 0.236 → 0.909, ceiling 0.927; no-answer
  leakage 16.7%). Affects the reranker-failure/fallback path and the Draft
  Room seam (rag_engine.py:4025 — pre-existing hardcoded `reranked=False`,
  the #510 antipattern, flagged as a follow-up, not changed here). Reranked
  paths (the deployed default) bypass this filter entirely.
- `.env.example`, `docs/release.md`, `backend/tests/test_config.py`,
  `backend/app/api/routes/chat.py` (comment) — consistency updates for the
  new default and the documented score_type contract.
- New evidence artifacts: `backend/tests/eval/calibration_2026_09/` (frozen
  63-query dataset, raw score dump, analysis, standalone validator) and
  `docs/eval/2026-09-*` (model qualification A/Bs, twelve-user performance,
  model-research verification), each with raw data JSON.

## Rollout

No global model defaults changed in this PR — the embedding model
(harrier-oss-v1-0.6b), reranker (bge-reranker-v2-m3), and every serving
endpoint are untouched; the E05 challenger (Qwen3-Reranker-0.6B) was
measured and REJECTED (gold-recall 0.896 vs 0.979), and the Harrier
query-prefix A/B (recall +0.036, MRR +0.069) is positive but below the
pre-declared 0.05 bar, so it is recorded as a recommended controlled
deployment trial (`EMBEDDING_QUERY_PREFIX`), not a default flip.

Carve-out — the ONE calibrated default that DID change in this PR:
`max_distance_threshold` 0.5 → 0.75 (backend/app/config.py), per the frozen
calibration dataset. Measured impact is limited to distance-typed records on
non-reranked fallback paths and the Draft Room seam (the deployed default chat
path reranks and is unaffected); the similarity-score fallback branch is pinned
at its legacy 0.5 floor via FALLBACK_SCORE_FLOOR and is NOT affected by the
move. The backend cutoff (0.75) sits just inside the UI's Related band
(Related ≤ 0.77, Tangential > 0.77): the UI labels the 0.75–0.77 tail Related
while the backend still emits it, and drops only beyond 0.75.

## Rollback

Set `MAX_DISTANCE_THRESHOLD=0.5` in the deployment environment to restore the
previous cutoff without a code change (env always wins over the default).
Frontend bands roll back with the code (no persisted state). The calibration
dataset and validator are additive. Any future embedding-model swap must go
through the identity-sidecar + dimension-rebuild path
(backend/app/services/vector_store.py:2174-2315) with rollback to the exact
prior model/index/config — referenced, not exercised, here.

## Evidence

- Calibration (frozen, deployment-bound): backend/tests/eval/calibration_2026_09/
  (63 queries; tuning/held-out; distance/rerank/rrf distributions; derived
  cuts + held-out validation; `validate.py --scope …` re-runnable).
- E05 ablations + negative results: docs/eval/2026-09-model-qualification.md
  (+ raw JSON): prefix-only A/B, frozen-pool reranker A/B, contextual-chunking
  subset A/B, top-k/top-n grid.
- Twelve-user performance: docs/eval/2026-09-performance.md (+ raw JSON):
  tiers 1/4/8/12, long generation, bulk ingestion, with the tier-8→12
  queue-wait regime change called out.
- Model research (primary sources + access dates): docs/eval/2026-09-model-research.md.
- Blinded ChatGPT/Claude comparator runs: explicitly unresolved evidence per
  the issue's own clause ("availability limitations remain explicitly
  unresolved evidence, not parity proof") — comparator access was not
  available during execution; recorded, not claimed.
- Checkpoint anchor receipts published on issue #36 (latest:
  manifest=25b5c72769b5d208e93671c1e5b5fcdf13589259,
  semantics=088793d87e7b257e2804745129cd66fb121cd747).
