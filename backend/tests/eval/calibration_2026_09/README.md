# Calibration 2026-09 — relevance-score bands and backend cutoff (issue #36)

Frozen data + analysis backing the 2026-09 recalibration of
`frontend/src/lib/relevance.ts` and `backend/app/config.py`
(`max_distance_threshold`). Authored at the Phase 2.5 red checkpoint; the
numbers below are the spec the implementation must match.

## Files

- `calibration_queries.jsonl` — 63 labelled queries (frozen dataset).
- `score_dump.json` — per-query raw retrieval (distance + rrf) and rerank
  scores for the top results (frozen distribution evidence).
- `analysis.json` — quantile statistics per score type/population, derived
  cut points, and held-out validation rates.
- `validate.py` — standalone stdlib validator (`--scope
  dataset|distributions|cuts|e05|perf|research|release`), exit 0 iff valid.

## Methodology

1. **Dataset construction.** 63 queries derived from real deployed corpus
   content across the representative vaults CDP (2), Slack (6), Legacy (7)
   plus no-answer probes in CDP and Legacy. Coverage classes: exact-identifier,
   paraphrase, numeric-fact, procedure, no-answer. Each content query carries
   `gold_file_ids`/`gold_chunk_ids` provenance; queries are split into
   `tuning` (33) and `held-out` (30) partitions; no-answer queries are
   labelled as a third population for distribution contrast.
2. **Labeling model.** Each retrieved/reranked candidate was labelled
   gold / non-gold / no-answer per query: gold = candidate whose `file_id`
   is the query's gold provenance file (the query was derived from that
   file's sampled content); non-gold = any other retrieved candidate (a
   genuine mixture on this corpus — near-duplicate manuals — used as a
   diagnostic only, never as a negative label); no-answer = all candidates
   for no-answer queries.
3. **Collection.** One live pass per query through the deployed retrieval
   path; raw per-result scores dumped (distance per multi-scale entry,
   rrf_fuse score, sigmoid rerank score). Single-arm limitation: only the
   original (untransformed) query was replayed — thinking-mode
   query-transformation variants were not replayed (see
   `score_dump.json` `meta.note`).
4. **Derivation.** Cut points were derived from tuning-split quantiles and
   validated on the held-out partition (agreement rates in
   `analysis.json` `heldout_validation`). Decision rule per band below.

## Provenance binding (deployed configuration)

- Deployed commit: `281bd7149d2d09310a1087fd675509408f76e087`
- Embedding model: `microsoft/harrier-oss-v1-0.6b` (cosine distance)
- Reranker: `BAAI/bge-reranker-v2-m3` (TEI raw logits, single client-side
  sigmoid — the #511 score contract)
- Hybrid RRF k=60, retrieval recency weight 0.1
- Initial retrieval top_k 20 → reranker top_n 7
- Collected 2026-09-19 on host R640AI (172.16.50.159)
- `max_distance_threshold` at collection time: 0.5 (the pre-fix default)

## Chosen cuts and their derivation

- **distance bands 0.56 / 0.67 / 0.77** — Highly Relevant <= 0.56,
  Relevant <= 0.67, Related <= 0.77, else Tangential. Derived as
  q25(gold) / q75(gold) / max(q25(no-answer), legacy 0.5 backend filter) =
  0.559 / 0.671 / 0.765, rounded to 0.56 / 0.67 / 0.77 (the max() clause was
  inert for this dataset: 0.765 > 0.5). Held-out band agreement: 74.4% (new)
  vs 30.0% (old 0.15/0.3/0.5 cuts).
- **rerank bands 0.7 / 0.4 / 0.2 kept** — the shipped cut points are
  retained now with recorded provenance: held-out agreement for the shipped
  cuts is tied-best (85.0%) vs the best-derived candidate (C2-posterior
  0.957/0.473/0.181 at 84.1%), so no change is justified by the data. The
  Tangential floor (0.2) is supported by the no-answer rerank distribution
  q95 = 0.181. Two derived candidates were evaluated and rejected: the
  naive quartile candidate (0.974/0.715/0.29, held-out 77.9%) and the
  C2-posterior candidate (84.1%); see `analysis.json`
  `derived_cuts.rerank.note` and `heldout_validation`.
- **backend cutoff 0.75** — `max_distance_threshold` default moves
  0.5 → 0.75. Gold-recall on the non-reranked path: 0.236 at threshold 0.5 →
  0.909 at 0.75 (ceiling 0.927 at no filter); no-answer leak at 0.75 is
  16.7%. 0.75 also sits at the Related/Tangential boundary so UI language
  and backend filtering agree.
- **rrf branch removed** — the backend score_type contract produces only
  "distance" and "rerank" (never "rrf"), and raw rrf_fuse scores top out at
  ~2/61 ≈ 0.033 (k=60, two lists; every observed rank-1 = 1/61 ≈ 0.0164),
  below even the old >= 0.05 "Moderate Match" floor: dead and degenerate.
  The `rrf` branch is removed from `relevance.ts` and `ScoreType` becomes
  `"distance" | "rerank"`; unknown/undefined scoreType keeps falling back to
  distance semantics.
