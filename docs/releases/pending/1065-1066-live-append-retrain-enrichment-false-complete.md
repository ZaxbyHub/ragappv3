# 1065 + 1066: live-append IVF_PQ retrain removed, enrichment failures stop storing complete

## Summary

Collated closure of the two ingestion hot-path issues:

- **Live appends no longer retrain the whole vector index (#1065).** Every
  live append to a table at or above 256 rows with an existing IvfPq index
  used to run a full-corpus `create_index(..., replace=True)` under the write
  lock — the skip predicate compared the mutation generation the append
  itself had just incremented, so it could never match. The append path
  (`add_chunks`, `add_chunks_then_delete_ids`) now accepts an already-built
  index and records that decision, exactly like the below-threshold delete
  branch: appended rows are served by LanceDB's brute-force scan over
  un-indexed fragments. Full retrains still happen at the first create
  (≥256 rows, no index) and at delete churn ≥ `index_rebuild_delta`; the
  search path keeps its rebuild/retry semantics, so a failed churn rebuild
  still retries on the next search (pinned by
  `test_default_call_rebuilds_when_bookkeeping_is_stale`). Known trade-offs
  (disclosed): (1) if a churn rebuild fails and an append interleaves before
  the next search, the append's accept subsumes that retry until the next
  churn-qualifying event; (2) an enrichment swap whose DELETE-side churn
  C/(table+file rows) ≥ `index_rebuild_delta` (a large file relative to the
  table) still pays ONE full retrain through the preserved delete-churn floor
  — the pre-fix code paid two for the same swap.
- **Failed or truncated enrichment is never stored as complete (#1066).** A
  curator response whose JSON does not parse, whose completion stopped at the
  512-token cap (`finish_reason=length`, including a cut-but-closed JSON
  object), or whose model call raised, is now stored as
  `enrichment_status='error'` with a non-empty `enrichment_error` (stable
  `ENRICHMENT_FAILED` code plus a `failed/total` count) instead of
  `complete`/NULL. One failed candidate fails the file's enrichment status
  (mixed batches included); successful siblings keep their already-searchable
  base rows because the swap is skipped. The failure write is
  currency-guarded like the swap tail: a superseded attempt marks itself
  stale instead of overwriting a newer job's status. The new per-call
  `metrics_sink` kwarg on `LLMClient.chat_completion` mirrors a call's
  finish_reason into a caller-owned dict (the shared `last_metrics` slot is
  unsafe to read after `await` under the enrichment service's concurrency),
  and the legitimate no-ops (enrichment disabled, no candidates, blank chunk
  text, a parsed-but-empty object) still store `complete`.

## Tests

- `backend/tests/test_live_append_ivfpq_retrain.py` (frozen spec, 4 tests):
  second live add and the enrichment-swap primitive accept a fresh IvfPq
  index without `replace=True`, plus below-256 and first-create floors.
- `backend/tests/test_enrichment_false_success.py` (frozen spec, 3 tests):
  unparsed/length/mixed batches and raised calls never store `complete`;
  a parsed-empty object still does.
- `backend/tests/test_enrichment_success_contract.py`: real-service success
  leg (closed JSON + finish stop → `complete`, summary swapped into the
  vector record) and the superseded-failure currency leg.
- `backend/tests/test_llm_metrics_sink.py`: the real client's per-call sink
  mirror (stop/length, provider token counts, per-call isolation).
- `backend/tests/test_deferred_index.py` gains the search-retry guard
  (`test_default_call_rebuilds_when_bookkeeping_is_stale`): stale bookkeeping
  + existing IvfPq index + the DEFAULT `_maybe_create_vector_index()` call
  must still rebuild with `replace=True` — only the append path accepts an
  existing index.

Persisted-shape note (additive): `ChunkEnrichment.to_dict()` now includes an
`"error"` key (None on success) in the enrichment metadata stored on enriched
vector rows; no consumer asserts an exact shape, and successful rows simply
carry `error: null` alongside the existing fields.
- Retargeted: `test_existing_embedding_index_is_refreshed_after_new_rows`,
  `test_delete_then_add_same_row_count_still_refreshes_existing_index`
  (test_ingestion_integrity.py) and the `await_count` assertion in
  `test_vector_store_async.py` now pin the no-retrain contract.

Closes #1065, closes #1066.
