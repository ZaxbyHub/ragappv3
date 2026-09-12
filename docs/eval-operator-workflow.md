# Evaluation Operator Workflow

How an operator turns a real vault into trustworthy evaluation evidence for
the deterministic harness (issue #237). The deterministic harness itself
runs in CI with synthetic fixtures; **operator data gates real-quality
conclusions** — no fabricated gold, no green empty benchmark.

## 1. Label representative real-corpus questions

1. Pick 20–50 questions a real user actually asked (or would ask) of the
   vault, spanning document retrieval, memory, wiki and no-match paths.
   (F2 expands this labeled pool to at least 50 cases.)
2. For each question, record the ground truth:
   - the file ids / chunk uids that should be retrieved
     (`expected_chunk_ids` / `expected_source_labels`),
   - optional memory / wiki / artifact expectations,
   - an optional reference answer (`expected_facts` — short, checkable
     facts, not prose),
   - `expect_no_match` for questions the system must answer with "no match".
3. Add each labeled case to a golden JSONL dataset using the versioned
   schema (`tests.eval.golden_schema.dump_cases`); the loader rejects
   duplicate ids and unknown schema versions.
4. Quality over quantity: every wrong label poisons every metric computed
   from it. Have a second person spot-check 10% of the labels.

## 2. Keep tuning and held-out apart

Assign every case id to exactly one split in a dataset manifest
(`fixtures/dataset_manifest.json` shape: `tuning_case_ids`,
`held_out_case_ids`). The loader enforces disjointness, non-emptiness and
that every manifest id exists — run experiments and any threshold tuning
against the **tuning** split only, and report final numbers from the
**held-out** split. Reusing held-out cases for tuning silently converts
your evaluation into a fitted score.

## 3. Run the deterministic harness

```
cd backend
python -m tests.eval.run_eval \
  --golden tests/eval/fixtures/golden.jsonl \
  --results <your results JSONL> \
  --manifest tests/eval/fixtures/dataset_manifest.json \
  --out report.json
```

The report states actual sample counts, per-metric denominators
(`metric_n`, `metric_skipped`), deterministic uncertainty intervals
(`uncertainty`, method `normal_approx_95_clamped`) and metric definitions.
Lexical-overlap numbers from `/api/eval/heuristic` are sanity checks, not
calibrated truth — never report them as reference-based metrics.

## 4. Compare runs before/after a change

`app.services.eval_compare.compare_runs` loads two run reports (each
stamped with code/model/config/corpus hashes via
`environment_fingerprint`) and returns per-metric deltas over matched
resources with intervals carried through. Identical runs compare to a zero
delta; a retrieval-affecting change (chunking, fusion weights, reranker,
embedding model) must show its delta on the affected metrics — and nothing
else moved. Commit each `/api/eval/live` run JSONL per experiment
(`data/eval-runs/runs.jsonl`) so downstream consumers (F2 / #36) can
re-derive comparisons.

## 5. Optional: live judge evaluation (never in CI)

`app.services.eval_judge.JudgeAdapter` scores candidate answers through a
caller-injected judge client. Every judgment records the judge identity
(provider/model/prompt version), bias controls (seeded position
randomization, optional cross-family judge) and human-calibration
metadata. Calibrate against a human-labeled panel before trusting judge
scores, and keep judges out of deterministic CI entirely.

## 6. User reports as evaluation cases

Users report quality problems from the chat UI (`incorrect_answer`,
`missing_source`, `stale_source`, `bad_extraction`), bound to the exact
message identity and provenance. Operators convert a report
(`POST /api/quality/reports/{id}/convert`) into a replayable evaluation
case with an expected outcome, then compare before/after replays
(`POST /api/quality/eval-cases/{id}/compare`) — a reported failure must be
replayable against the same inputs and comparable after the fix.

## Trace-based evaluation (E09, deferred)

Evaluating over OTLP traces (question, retrieved contexts, answer captured
per turn) is deferred to issue #518, which owns the OTel telemetry carrier.
Retention/privacy decision: trace payloads must carry identifiers and
counts only until #518 lands — `rag_trace.py` logs identifiers/counts, not
query text or context bodies; extending trace payloads to full evaluation
inputs is a deliberate retention decision that #518 must make explicitly,
not a side effect of this evaluator.
