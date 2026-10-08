# 704 — Ingest status/error signals and the shared write permit match their documented contract

**Issue:** #704 (Workstream B, PR 15 of 16; findings T1-25-KR-02, T1-25-K-02, T1-25-S2-04, T1-25-KR-16, T1-25-KR-06 residual, T1-25-KR-14, T1-25-KR-15, T1-25-S2-10, T1-25-KR-09, T1-25-KR-11)

## Outcome

The ingest pipeline's status and error signals now say what actually happened,
and the shared SQLite write permit now covers every committing write it claims
to cover. Operators can distinguish an embedding-provider outage
(`EMBEDDING_FAILED`) or a vector-store rejection (`VECTOR_STORE_FAILED`) from a
genuine parse failure (`PARSE_FAILED`) in `files.error_message`; a `partial`
file whose failed chunks all recover is promoted back to `indexed`; a failed
re-ingest that predates any vector write restores the prior terminal status
instead of demoting a fully-searchable file to `error`; success finalization
commits `status` and `parsed_text` in one transaction; a compensated
artifact-publish failure leaves a durable `stage='publish',
status='failed_retryable'` marker in `ingestion_stage_states`; the dead
`enrichment_ms` timing key and two lying docstrings/log lines are gone; the
`@with_retry` on the scan/upload row insert can actually retry transient
`sqlite3.Error`s; and the AST census of raw pool checkouts inside committing
functions (13 at the audit snapshot, 16 at base with #783/#703-era additions)
is now zero, guarded by a frozen census test.

## What changed

- `backend/app/services/document_processor.py`:
  - `classify_ingest_error` classifies by failure provenance — new arms map
    `EmbeddingError` → `EMBEDDING_FAILED` and `VectorStoreError` →
    `VECTOR_STORE_FAILED` (registered in `_INGEST_ERROR_REASONS` and the
    `SHIPPED_INGEST_ERROR_CODES` census; `DIMENSION_CHANGED` keeps precedence).
  - The chunk-retry cleanup `UPDATE` recomputes `status`: zero remaining
    `failed_chunks` rows promote a `partial` file back to `indexed`.
  - Both ingest entry points capture the row's pre-ingest terminal status and
    a prior-generation-disturbed boundary (the vector-write stage); a failure
    before that boundary restores `indexed`/`partial` (guarded `UPDATE`, so a
    concurrent cancel/settle still wins) instead of writing `error`.
  - `_finalize_indexed_success` commits `parsed_text` in the same transaction
    as the terminal status; the wiki-pending set/clear now run inline on one
    session-held connection (removing nested raw checkouts that would
    deadlock under the permit).
  - `_insert_or_get_file_record` splits into a retried single-attempt body
    (`_insert_or_get_file_record_once`, no interior `sqlite3.Error` re-typing)
    plus a public wrapper that re-types only what survives the retries;
    `IntegrityError` keeps its `DuplicateFileError` semantics.
  - `_publish_artifacts`/`_tombstone_materialized_assets` take an optional
    `conn` (production callers pass a `_write_session`-held connection; the
    `None` fallback is test/legacy-only with explicit own-conn ownership), and
    a compensated publish failure records the durable marker via the new
    `artifact_store.record_stage_failure` in both except branches.
  - `enrichment_ms` dropped from `_STAGE_TIMING_FIELDS` and both timing logs
    (it was never recorded — structurally always 0.0); the no-text image log
    line no longer claims the file is "recorded" when the caller fails it.
  - `rollback_cancelled_ingest`, `_retry_failed_chunks_locked`, the
    failed-chunk persist and >50%-abort cleanups, the finalize reread, and the
    enrichment setters (`set_enrichment_status` /
    `_mark_enrichment_stale_if_current_job`, now async) all route through the
    shared write permit.
- `backend/app/services/document_progress.py`: new module-level
  `write_session(pool, permit=None)` — the single permit-first /
  checkout-inside / release-order implementation — used by
  `set_phase`/`clear_progress`/`set_wiki_pending` and delegated to by
  `DocumentProcessor._write_session`. The permit is carried on the pool
  (`pool.write_permit`, installed by the BackgroundProcessor that owns the
  semaphore) so module-level helpers share the exact permit; discovery probes
  the instance dict so mock pools cannot fabricate one.
- `backend/app/services/contextual_chunking.py`: `_contextualize_single_chunk`
  docstring now matches the code (`contextualized=False` on LLM failure).
- `backend/app/services/artifact_store.py`: `record_stage_failure` — durable,
  schema-admitted (`failed_retryable`), content-free stage-failure rows.
- Tests: frozen `tests/test_b15_ingest_signals.py` (12 acceptance checks C1-C12,
  9 RED→GREEN, 3 PRESERVING GREEN→GREEN), non-frozen shape pins in
  `tests/test_b15b_publish_marker_shape.py`, the new codes registered in the
  #562 census guard, and the `enrichment_ms` pin updated in
  `tests/test_safe_concurrent_ingestion.py`.

## Notes

- `ingestion_stage_states.status` admits `failed_retryable` already — no
  schema change (the issue pins `Schema after: none`).
- `tests/test_m03_ingest_cancel_companion.py::test_process_file_path_cancel_gate`
  is a pre-existing startup-recovery race (recovery re-enqueues a live
  scan-path row once its phase leaves the parse trio and the re-enqueue's
  `clear_cancel` discards the test's cancel): reproduced failing 1/3 runs at
  base 056171aa with the identical signature; this diff shifts its local
  odds (2/3) without changing the mechanism. Disclosed in the PR.
