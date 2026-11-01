# fix(rag): rendered evidence matches what retrieval decided (#716)

## What changed

Workstream C PR 11 of 14 (audit findings TQ-instruction-batch-4-05/09,
T1-35-K-01/07/09/10, T1-35-S2-04/06, plus the escape-parity item absorbed
from the #966 review). Nine fixes so the text, scores, and diagnostics the
model or operator sees stop disagreeing with what upstream decided.

- **MATCH fidelity.** `format_chunk` renders the `[[MATCH: ...]]` region from
  post-pipeline `chunk.text` instead of pre-distillation `metadata.raw_text`,
  so sentences the distiller or wiki-overlap suppression removed can no
  longer be re-injected; the in-place branch also gained whitespace-
  tolerant containment — the distiller rebuilds chunk text with normalized
  single-space joins while the parent window is a raw document slice, so
  the exact-containment test failed on the default config and
  multi-sentence chunks fell to the duplicate-appending fallback.
- **Single sigmoid on the local rerank path.** `_rerank_local` passes a
  torch-free identity `activation_fn` to `CrossEncoder.predict`, so
  sentence-transformers ≥ 5.6 does not pre-sigmoid and `_safe_sigmoid` is
  the single conversion — local-mode scores span the full (0,1) range again.
  Three raw-logit test fakes gained the kwarg (assertions unchanged).
- **Retired wiki pages stop serving.** All four page-joining surfaces
  (`_get_page_evidence`, `_fts_page_search`, `_relation_lookup`,
  `_fts_claim_search`) filter `status IN retired ('archived','stale')`; an
  active claim on a retired page is retired with it; orphan LEFT JOIN rows
  keep their existing semantics via the `IS NULL` branch.
- **Stored 0.0 confidence survives.** All five wiki confidence reads are
  None-aware (chain-aware at the relation two-key site), so a stored `0.0`
  renders and ranks as `0.0`, not a 0.6–0.8 default.
- **Rerank telemetry + hardening.** The rerank span advertises the
  configured `reranker_model` (was always empty); malformed TEI indices take
  the documented `(chunks[:n], False)` fallback (producer-side ValueError
  plus a consumption-layer backstop that also covers negative indices and
  the local path's enumerate positions).
- **Cache version skew.** `PROMPT_VERSION` is part of both the query-transform
  and query-plan cache keys, read at call time — a prompt change now
  invalidates cached rewrites by key change.
- **Multimodal aid bound.** The enrichment prompt states the 8-aid limit and
  the parser truncates to `MAX_AIDS` instead of permanently failing a
  compliant-looking response.
- **Support scoring normalized on the model side.** The model answer is
  passed to `score_citations` through `html.unescape`, so entity encoding
  cancels regardless of whether the model echoed the escaped prompt
  fragments or emitted natural characters (feedback probes showed scoring
  the escaped evidence side instead INVERTED the score for natural answers:
  0.875 → 0.4545 on the probe pair); all four label families' registries
  stay raw (absorbed from the #966 review).

## Known limitations

- The `[S#]` support registry still reads `raw_text` for its SOURCE (the
  pre-distillation text) — that source divergence is PR 9's disclosed
  limitation; this PR makes the comparison premise-invariant (model side
  unescaped) rather than changing the registry source.
- The `PROMPT_VERSION` key component invalidates previously cached rewrites
  by key change (no active flush runs): Redis entries age out by
  `query_transform_cache_ttl_sec`, while the in-memory LRU is size-bounded
  (1024 entries) and TTL-less, so its stale entries leave only via eviction.
