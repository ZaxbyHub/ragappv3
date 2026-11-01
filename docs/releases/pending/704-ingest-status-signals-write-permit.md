# 704 — Ingest status/error signals and the shared write permit match their documented contract

**Issue:** #704 (Workstream B, PR 15 of 16; findings T1-25-KR-02, T1-25-K-02, T1-25-S2-04, T1-25-KR-16, T1-25-KR-06 residual, T1-25-KR-14, T1-25-KR-15, T1-25-S2-10, T1-25-KR-09, T1-25-KR-11)

## Outcome

The ingest pipeline's status and error signals now say what actually happened,
and the shared SQLite write permit now covers every committing write it claims
to cover. Operators can distinguish an embedding-provider outage
(`EMBEDDING_FAILED`) or a vector-store rejection (`VECTOR_STORE_FAILED`) from a
genuine parse failure (`PARSE_FAILED`) in `files.error_message` — including
through the real failure paths: the >50% embedding-abort and the vector
visibility check carry their codes, and raw LanceDB write failures are wrapped
into the vector-store error family. A `partial` file whose failed chunks all
recover is promoted back to `indexed`; a failed re-ingest that predates any
vector write restores the prior terminal status **and content hash** (dropping
the aborted attempt's foreign chunk ledger) instead of demoting a
fully-searchable file to `error` or poisoning the duplicate check; success
finalization commits `status` and `parsed_text` in one transaction; a
compensated artifact-publish failure leaves a durable `stage='publish',
status='failed_retryable'` marker in `ingestion_stage_states` (operator-queryable
via SQL; a failed re-publish of a generation with committed rows writes no
marker, since nothing is stranded); publish compensation no longer tombstones
asset bytes that committed rows still reference (tombstone-side filter plus a
sweep-side reference skip); the dead `enrichment_ms` timing key and two lying
docstrings/log lines are gone; the `@with_retry` on the scan/upload row insert
can actually retry transient `sqlite3.Error`s; and the AST census of raw pool
checkouts inside committing functions of `document_processor.py` and
`document_progress.py` (13 at the audit snapshot, 16 at base with #783/#703-era
additions) is now zero, guarded by a frozen census test **scoped to those two
files** (other modules, e.g. `background_tasks.py`, legitimately keep their own
checkout patterns).

## What changed

- `backend/app/services/document_processor.py`:
  - `classify_ingest_error` classifies by failure provenance — new arms map
    `EmbeddingError` → `EMBEDDING_FAILED` and `VectorStoreError` →
    `VECTOR_STORE_FAILED` (registered in `_INGEST_ERROR_REASONS` and the
    #562 census; `DIMENSION_CHANGED` keeps precedence). The >50%
    embedding-abort error and the vector visibility-check error carry their
    codes on the real failure paths.
  - The chunk-retry cleanup `UPDATE` recomputes `status`: zero remaining
    `failed_chunks` rows promote a `partial` file back to `indexed` (both
    branches guarded to `indexed`/`partial`).
  - Both ingest entry points capture the row's pre-ingest terminal status
    **and content hash** plus a prior-generation-disturbed boundary (the
    vector-write stage); a failure before that boundary restores
    `indexed`/`partial` with the prior hash, zeroes the aborted attempt's
    counters, and deletes its foreign `failed_chunks` rows (guarded `UPDATE`,
    so a concurrent cancel/settle still wins). The terminal `error` write is
    likewise guarded, as is the worker's attempt-cap write and the recovery
    reset. Applies to the scan/reindex paths; upload-path rows are reset to
    `pending` by the route before the worker sees them, so those failures
    land `error` as before. Restored rows intentionally keep a redacted
    `error_message` describing the failed attempt (cleared on the next
    success).
  - `_finalize_indexed_success` commits `parsed_text` in the same transaction
    as the terminal status; `wiki_pending=1` commits first (keeping the
    base-observable "pending" window) before the job-creation transaction.
  - `_insert_or_get_file_record` splits into a retried single-attempt body
    plus a public wrapper that re-types only what survives the retries;
    `IntegrityError` keeps its `DuplicateFileError` semantics.
  - `_publish_artifacts` takes an optional `conn` (production callers pass a
    `_write_session`-held connection) and an optional pre-materialized asset
    list — production callers materialize asset bytes BEFORE the session so
    synchronous disk I/O does not extend the permit's hold; publish
    compensation filters still-referenced `rel_paths` out of the tombstone
    set and skips the marker when the generation's rows are already
    committed.
  - `enrichment_ms` dropped from `_STAGE_TIMING_FIELDS` and both timing logs
    (it was never recorded — structurally always 0.0); the no-text image log
    line no longer claims the file is "recorded" when the caller fails it;
    the enrichment-cancel status write runs as a referenced detached task.
- `backend/app/services/document_progress.py`: new module-level
  `write_session(pool, permit=None)` — the single permit-first /
  checkout-inside / release-order implementation — used by
  `set_phase`/`clear_progress`/`set_wiki_pending` and delegated to by
  `DocumentProcessor._write_session`. The permit is carried on the pool
  (`pool.write_permit`, installed by the BackgroundProcessor that owns the
  semaphore and cleared on stop) so module-level helpers share the exact
  permit; discovery probes the instance dict so mock pools cannot fabricate
  one. Note: route-path progress writes now wait on this permit while a
  worker holds it (the centroid scan's permit-held `to_thread` predates this
  change); holds are bounded by SQLite transaction time.
- `backend/app/services/background_tasks.py`: pool-carried `write_permit`
  wiring at start/teardown **and stop**; the attempt-cap terminal write and
  the recovery reset are status-guarded.
- `backend/app/services/contextual_chunking.py`: `_contextualize_single_chunk`
  docstring now matches the code (`contextualized=False` on LLM failure).
- `backend/app/services/artifact_store.py`: `record_stage_failure` — durable,
  schema-admitted (`failed_retryable`), content-free stage-failure rows — and
  a sweep-side reference check so pending tombstones are never unlinked while
  a committed `document_assets` row still references the path.
- `backend/app/services/vector_store.py`: raw LanceDB `add`/`merge_insert`
  write failures and the ingest-path "Table not initialized" error are
  wrapped into the `VectorStoreError` family so they classify
  `VECTOR_STORE_FAILED`.
- Tests: frozen `tests/test_b15_ingest_signals.py` (12 acceptance checks
  C1-C12, 9 RED→GREEN, 3 PRESERVING GREEN→GREEN), non-frozen shape pins in
  `tests/test_b15b_publish_marker_shape.py` (marker shape via a FRESH
  connection, re-raising branch, ownership/no-leak), review pins in
  `tests/test_b15c_review_round1.py` (vector-store arm, guarded attempt-cap
  write), and the feedback battery `tests/test_b15d_feedback.py` (end-to-end
  outage classification through both entry points, process_file restore leg,
  partial-prior restore, post-disturbance failure lands `error`, permit
  install/clear behaviour, marker skip on failed re-publish, tombstone
  reference filter, retry exhaustion, `DIMENSION_CHANGED` precedence); the
  new codes registered in the #562 census guard; `enrichment_ms` pin updated
  in `tests/test_safe_concurrent_ingestion.py`.

## Notes

- `ingestion_stage_states.status` admits `failed_retryable` already — no
  schema change (the issue pins `Schema after: none`).
- The `bandit-baseline.json` re-anchor is semantically pure (identical
  finding multiset; line numbers only) but also flips the file CRLF→LF;
  `scripts/run_bandit.py` now writes LF explicitly so regeneration is
  idempotent.
- Parts of the diff are canonical `ruff format` reflow in already-touched
  files; semantic edits review cleanly with `git diff -w`.
- `tests/test_m03_ingest_cancel_companion.py::test_process_file_path_cancel_gate`
  is a pre-existing startup-recovery race (recovery re-enqueues a live
  scan-path row once its phase leaves the parse trio and the re-enqueue's
  `clear_cancel` discards the test's cancel): reproduced failing at base
  056171aa with the identical signature; the rate is machine/load-dependent
  (observed 1/3 at base under load, all-green head samples elsewhere) and the
  mechanism is unchanged by this diff.
- Two known corners documented rather than fixed: upload-path rows cannot
  benefit from the AC3 restore (the route resets them to `pending` before the
  worker runs — restore applies to scan/reindex), and a restored `partial`
  row loses its chunk-retry ledger (the ingest start deleted it; the row
  reports zero remaining chunks and a full re-ingest recovers).
