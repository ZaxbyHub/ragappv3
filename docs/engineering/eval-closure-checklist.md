# Evaluation Closure Checklist (issue #237)

Every field carries a real value — an empty or placeholder threshold/sample
field is a closure blocker by definition. Operator-gated entries state the
actual pending condition (a labeled count of 0 today, with the target it
must reach), never a placeholder.

## Deterministic harness gates (CI, synthetic fixtures)

| Metric | Threshold | Sample size | Status |
|---|---|---|---|
| Deterministic metric math (recall@k, MRR, nDCG@k, citation validity, fact coverage) | exact expected values in unit fixtures (0.0/0.5/1.0 cases pinned) | 15 fixture cases + per-primitive unit tests | met at HEAD |
| ran_count honesty (specialized cases counted by result presence) | ran_count equals completed-case count (3 of 4 in the pinned scenario; absent case excluded) | 4-case regression fixture | met at HEAD |
| Duplicate benchmark/goldener id rejection | duplicate ids raise a validation error (HTTP 422 at the API; ValueError at the adapter/loader) | 2 duplicate-id regression fixtures | met at HEAD |
| Negative cosine tolerance | answer_similarity on [-1, 1] returns 200 for -1.0, 0.0 and +1.0 | 3-value route regression fixture | met at HEAD |
| Empty-passage citation invalidity | valid_count 0 / accuracy 0.0 for empty and whitespace-only passages; 1.0 for a real passage; 1.0 for an empty citation list | 4-fixture scorer regression | met at HEAD |
| Tuning/held-out disjointness | overlapping, unknown, or empty splits raise ValueError; fixture manifest is disjoint and non-empty | 15-case fixture manifest | met at HEAD |
| Baseline comparison exactness | identical runs give a 0.0 delta on every metric; a controlled regression moves exactly the affected metric | 2-metric comparison fixture (identical-runs zero delta + controlled regression; tests/test_eval_compare.py) | met at HEAD |
| Semantic preservation decision table | paraphrase passes without literal substring; negation, numeric and code contradictions fail | 11-assertion decision-table suite (tests/test_semantic_checks.py) | met at HEAD |
| Report honesty | every mean carries metric_n, metric_skipped, an uncertainty interval and a definition; no lexical heuristic is labeled as an external-library metric | 15-key report contract fixture | met at HEAD |
| Deterministic retrieval gold corpus (issue #658) | retrieval discrimination on confusable fixtures: expected spans in top-k via real hybrid search, near-traps never ranked above expected passages, out-of-domain queries return no confident hit | 25 pinned cases over 10 sha-pinned fixtures; runs deterministically in CI (Backend job, ~8s; `tests/test_retrieval_gold_corpus.py`) | met at HEAD |

## Real-quality gates (operator evidence — the issue's own operator-data gate)

| Gate | Threshold | Sample size | Status |
|---|---|---|---|
| Real-corpus labeled questions | recall@k and MRR reported from the held-out split only, with uncertainty intervals, before any retrieval-affecting change is declared an improvement | 20–50 labeled cases (F2 expands to at least 50); today 0 labeled real cases exist — pending operator labeling per the workflow in docs/eval-operator-workflow.md | pending operator data |
| Human calibration of the live judge | judge–human agreement rate at or above 0.80 on the calibration panel before judge scores appear in any report | 40 human-labeled judgments per calibration round; 0 labeled to date — pending operator panel | pending operator data |
| Held-out discipline on experiments | zero held-out cases used for tuning in any recorded experiment | every experiment run; enforced by the manifest loader | met by construction |

The deterministic retrieval gold corpus (issue #658) closes the deterministic
half of this table's first row: synthetic, sha-pinned retrieval cases now run
in CI. The real-corpus labeled split above remains pending operator data —
deterministic synthetic cases are not labeled real cases, and the operator
workflow (docs/eval-operator-workflow.md section 3a) states what the corpus
does and does not prove.

## Interpretation rules

- Lexical-overlap heuristics (`/api/eval/heuristic`) are sanity checks;
  they are never reported as calibrated or reference-based metrics.
- An absent result, an unavailable retrieval and an empty ranking are three
  different states; the report keeps them distinct (status counts, skipped
  denominators, means excluding outages).
- A delta is only interpretable when matched resources and environment
  hashes are recorded; comparisons without them are not evidence.
