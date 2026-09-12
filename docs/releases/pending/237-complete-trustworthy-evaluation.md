# fix(eval): trustworthy evaluation — repairs, honest reporting, comparison, and report-to-case loop (#237)

## What changed

- **EVAL-001** — `/api/eval/heuristic` (renamed from `/api/eval/ragas`, which
  stays as a deprecated alias per closed issue #343) now accepts a negative
  embedding cosine: `answer_similarity` is documented and validated on
  `[-1, 1]` instead of crashing with HTTP 500 on anti-correlated pairs.
- **EVAL-002** — duplicate benchmark item ids are rejected with HTTP 422 at
  `/api/eval/live` request validation, and `LiveEvalAdapter.score_run`
  raises `ValueError` for duplicate benchmark ids and duplicate ranking
  query ids (no more silently scoring one query against another's ranking).
- **EVAL-003** — `ran_count` is derived from result presence
  (`CaseMetrics.ran`), so completed memory-only, no-match and artifact-only
  cases are counted; unavailable-vs-absent stays distinguishable.
- **EVAL-004** — the gold citation scorer no longer credits empty or
  whitespace-only passages as valid citations (empty citation *lists* still
  score 1.0, as documented).
- **Honest reporting** — `EvalRunner.summarize()` adds `metric_n`,
  `metric_skipped`, deterministic `uncertainty` intervals
  (`normal_approx_95_clamped`) and `metric_definitions`; the frozen 15-key
  mean contract lives in `tests/eval/report_contract.py`.
- **Golden schema + split discipline** — versioned JSONL serialization with
  round-trip validation (`tests/eval/golden_schema.py`), duplicate-id
  rejection in the golden loader, and tuning/held-out dataset manifests
  with enforced disjointness (`tests/eval/dataset_manifest.py` +
  `fixtures/dataset_manifest.json`; `run_eval.py --manifest`).
- **Baseline comparison** — `app/services/eval_compare.py` loads two run
  reports (code/model/config/corpus hashes, matched resources, intervals)
  and computes per-metric deltas; identical runs compare to exactly zero.
- **Live judge adapter (opt-in)** — `app/services/eval_judge.py` records
  provider/model/prompt-version identity, seeded position randomization and
  cross-family configuration, and human-calibration metadata; network-free,
  never invoked from CI.
- **Semantic preservation checks** — `app/services/semantic_checks.py`
  passes correct paraphrases without literal substring presence and fails
  negation, numeric and code-identifier contradictions.
- **PRODUCT-ENH-12** — structured quality reports (`/api/quality/reports`,
  categories: incorrect_answer / missing_source / stale_source /
  bad_extraction) bound to turn identity and provenance, operator
  report-to-case conversion, and before/after replay comparison; a
  `QualityReportDialog` on the assistant-message actions submits them.
- **Docs/config** — `docs/eval-operator-workflow.md` (labeling workflow,
  E09 retention decision deferred to #518),
  `docs/engineering/eval-closure-checklist.md` (no empty threshold/sample
  fields), `EVAL_ENABLED` documented in `.env.example`, admin-guide
  updated for the renamed route and new surfaces.

## Why

Issue #237 (Workstream F, PR 1 of 3) required the evaluator to be
reproducible, correctly counted and comparable; to separate missing service
from bad recall; to gate deterministically in CI while keeping live judges
explicit and calibrated; and to close the loop from user complaints to
replayable evaluation evidence. EVAL-001..004 were confirmed live at HEAD
a543361 and are repaired with regression tests that fail on revert; the
route rename resolves the #343 record/code contradiction without breaking
existing callers.

## Migration steps

- The two new tables (`quality_reports`, `quality_eval_cases`) are created
  by idempotent migrations on startup (`run_migrations`); no manual step.
- `/api/eval/ragas` continues to work unchanged (deprecated alias);
  clients should move to `/api/eval/heuristic`.
- Eval reports gain new summary keys additively; old keys are unchanged.
  Old saved reports load as before; new reports carry the run envelope.

## Breaking changes

- None at the HTTP boundary. `RAGAS*` model names in
  `app.api.routes.eval` are renamed to `Heuristic*` (module-level surface;
  tests updated accordingly — no external consumers in the repo).

## Known caveats

- Operator real-corpus labels (20–50 cases) remain pending operator
  evidence by design; the deterministic harness and workflow ship now and
  operator data gates real-quality conclusions only.
- The live judge is operator tooling: it performs no network I/O itself and
  must be calibrated against a human panel before its scores are trusted.
- Trace-based evaluation over OTLP is deferred to issue #518 (OTel owner);
  the retention/privacy decision is recorded in the operator workflow doc.
