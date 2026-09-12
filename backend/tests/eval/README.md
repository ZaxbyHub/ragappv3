# RAG evaluation harness

A lightweight, CI-safe harness for measuring retrieval and citation
quality of the RAG pipeline.

## Why

Unit tests prove the code paths are wired; this harness measures
**quality** end-to-end against a golden set. Defaults can only be
tuned with evidence (per the task instructions) — this is the evidence
generator.

## Components

* `eval_harness.py` — the metric primitives (recall@k, MRR, nDCG@k,
  citation validity, fact coverage — the latter two re-exported from
  `app.services.eval_metrics`) plus the `EvalRunner` aggregator.
* `run_eval.py` — CLI that consumes a golden JSONL + a results JSONL
  and emits a summary + machine-readable JSON report. `--manifest`
  validates a tuning/held-out dataset manifest and reports per-split
  counts; `--out` additionally writes a `run_report` envelope loadable
  by `app.services.eval_compare.load_run_report` for baseline/candidate
  comparison.
* `golden_schema.py` — versioned JSONL serialization (`GOLDEN_SCHEMA_VERSION`)
  with round-trip guarantees for cases and results; `parse_*` reject
  unknown schema versions and duplicate ids.
* `dataset_manifest.py` + `fixtures/dataset_manifest.json` —
  tuning/held-out split assignment with enforced disjointness,
  non-emptiness, and unknown-id rejection.
* `report_contract.py` — the frozen 15-key mean-metric contract and the
  documented uncertainty method (`normal_approx_95_clamped`) plus metric
  definitions.
* `fixtures/golden.jsonl` — a tiny dataset that exercises every
  scoring path (retrieval recall, citation, memory recall, wiki recall,
  no-match).
* `test_eval_harness.py` (under `backend/tests/`) — unit tests over the
  metric math; `test_golden_schema.py`, `test_dataset_manifest.py`,
  `test_run_eval.py` (this directory) cover the new modules and the CLI.

## Golden case shape

See `eval_harness.GoldenCase` for the authoritative field set. Current
fields (all optional except `id` and `query`):

```json
{
    "id": "case-001",
    "vault_id": 1,
    "query": "What is the project deadline?",
    "expected_chunk_ids": ["chunk-1", "chunk-2"],
    "expected_source_labels": ["S1", "S2"],
    "expected_facts": ["October 15"],
    "expected_memories": ["M1"],
    "expected_wiki_labels": ["W1"],
    "expect_no_match": false,
    "expected_artifact_ids": ["art-1"],
    "expected_modalities": ["text"],
    "expected_vision_mode": "either"
}
```

The artifact/modality/vision fields come from issue #462 (multimodal
evaluation). A case with no `expected_chunk_ids` simply has `recall@k`,
`MRR`, and `nDCG` recorded as `null` so the summary doesn't average
them in. Duplicate `id`s are rejected by the loader.

## Result shape

See `eval_harness.CaseResult` for the authoritative field set. Current
fields:

```json
{
    "id": "case-001",
    "retrieved_chunk_ids": ["chunk-1", "chunk-7"],
    "retrieved_source_labels": ["S1", "S2"],
    "answer": "We shipped X [S1] and Y [S2].",
    "cited_source_labels": ["S1", "S2"],
    "cited_memory_labels": [],
    "cited_wiki_labels": [],
    "invalid_citations": [],
    "no_match_returned": false,
    "retrieved_artifact_ids": [],
    "cited_artifact_ids": [],
    "retrieved_modalities": [],
    "retrieval_status": "ok",
    "vision_used_artifact_ids": [],
    "vision_degraded_artifact_ids": []
}
```

`retrieval_status` is `"ok"`, `"partial"`, or `"unavailable"`; an
`unavailable` case is excluded from metric means but counted in the
status distribution (an outage is never folded in as a 0.0).

The `summarize()` report adds honest-reporting keys on top of the
classic means: `ran_count` (result presence), `metric_n` /
`metric_skipped` (per-metric denominators), `uncertainty` (deterministic
intervals, method `normal_approx_95_clamped`), and `metric_definitions`
(see `report_contract.py`).

## Running

```bash
cd backend
python -m tests.eval.run_eval \\
    --golden tests/eval/fixtures/golden.jsonl \\
    --results path/to/your/results.jsonl \\
    --manifest tests/eval/fixtures/dataset_manifest.json \\
    --out eval-report.json
```

The summary is printed to stdout; the JSON report contains per-case
metric breakdowns, the split assignment, and a `run_report` envelope.

## Producing results

The harness intentionally does not call the live LLM or vector store
itself — wire it up however you like:

* **Mocked replay**: feed deterministic mock retrieval/citation
  output for fast CI smoke testing.
* **Live**: write a small adapter that runs `RAGEngine.query` for
  each golden case and records the actual retrieved chunk ids /
  citations into a results file.
* **A/B**: run the harness twice (with different config values) and
  compare the `run_report` envelopes with
  `app.services.eval_compare.compare_runs`.

## CI safety

Importing `eval_harness` is side-effect free. The unit tests in
`backend/tests/test_eval_harness.py` exercise every metric without
any backend dependency.
