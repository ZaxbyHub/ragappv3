# Workstream B PR 8: near-duplicate detection and enrichment-toggle resolution off the event loop (Issue #697)

## What changed

### Backend

- **`backend/app/models/database.py`** — `document_near_dups` rows now carry
  the `embedding_model` they were computed under (new nullable column in
  `_BASE_SCHEMA`; `migrate_add_document_near_dups` adds it to pre-existing
  tables via a PRAGMA-guarded `ALTER TABLE` and stamps legacy non-fingerprint
  rows (`dim != 256`, `embedding_model IS NULL`) with the currently-configured
  `settings.embedding_model`). Centroids are only compared in embedding space
  with rows recorded under the same model at the same dimension, so a
  same-dimension embedding-model switch invalidates old rows instead of
  silently comparing across spaces.
- **`backend/app/services/near_duplicates.py`** — `record_file_centroid`:
  - The embedding-space candidate SELECT filters on `(embedding_model, dim)`;
    the old `shape[0] != dim` Python skip remains only as a corruption guard.
  - The text-fingerprint fallback now routes EVERY non-comparable vault file
    through an explicit re-fingerprint path instead of the old silent
    exclusion: candidates are files with no `document_near_dups` row, or a
    row under a different (model, dim). Stored fingerprints (`dim = 256`,
    `embedding_model NULL` rows) are compared via the stored blob without
    re-tokenization; every other scanned candidate is fingerprinted once and
    its row is written in fingerprint form (marking it scanned), preserving
    any existing advisory group. A matched candidate's existing `group_id` is
    reused when non-NULL, else a fresh group is minted and backfilled onto
    both rows. No file is unreachable by both scan paths anymore: a 256-dim
    legacy row joins the group of an identical-text file recorded under a
    1024-dim model.
  - Every INSERT stamps `embedding_model` (`settings.embedding_model` for
    embedding rows; NULL for fingerprint rows).
- **`backend/app/services/document_processor.py`** — the advisory scan is
  dispatched with `asyncio.to_thread` inside the existing `_write_session`
  block (pool connections are `check_same_thread=False`), so ingest
  finalization no longer runs the scan's tokenization and SQLite I/O on the
  event loop. Both enrichment helpers (`is_enrichment_enabled_for_vault`,
  `is_enrichment_enabled_for_file`) now log a WARNING (with
  `exc_info=True`) naming the vault/file when their DB-error fallback fires,
  instead of silently overriding an explicit opt-out with the global setting.
- **`backend/app/api/routes/documents.py`** — `list_documents`,
  `get_document`, and `toggle_file_enrichment` resolve effective enrichment
  for their rows in ONE batched off-loop query
  (`_resolve_page_enrichment`: vault overrides for the page's distinct
  vault_ids, pre-seeded with the global default so orphaned vault ids
  resolve without falling back to the per-file helper). The map is passed
  into `_row_to_document_response` via the new `enrichment_map` keyword; the
  per-file synchronous helper remains only as a standalone fallback inside
  the builder, unreachable from async frames.
- **Tests** — `test_b08_off_loop_scans.py` (AC1/AC2/AC5), frozen checks:
  the centroid call runs off the loop through a real reprocess ingest, the
  second ingest re-fingerprints only its own text, and `list_documents`
  resolves enrichment with zero on-loop helper calls.
  `test_b08_near_dup_identity.py` (AC3/AC4), frozen checks: a legacy 256-dim
  row joins the identical-text file's group; centroids recorded under
  different embedding models are never grouped.
  `test_b08_enrichment_fallback_logging.py` (AC6/AC7), frozen checks: both
  helpers emit >= 1 WARNING record on a DB error.
  `test_b08_guardrail_sync_census.py` (new, non-frozen): scoped AST census —
  every production `record_file_centroid` reference must be
  executor-dispatched, and every async-frame
  `_row_to_document_response` call must pass a non-None `enrichment_map`
  (RED on base, GREEN on fix). The repo-wide structural sync-call guard is
  owned by #803; this census covers only #697's surfaces.
- **Docs** — `docs/releases/pending/645-on-loop-pooled-checkouts-repo-wide.md`
  Known limitations updated: the enrichment-resolution and near-dup-scan
  sites are converted; the remaining ingest-pipeline enrichment-helper
  reachability (`should_enqueue_enrichment`, `_get_chunk_enrichment_service`)
  stays pre-existing debt owned by Workstream B PRs 9/14/15.

### Operator caveats

- **One-time marking wave.** On the first ingests after deploying this change,
  each vault's unmatched files (no row, or a stale row) are fingerprinted once
  — up to `MAX_COMPARE = 500` tokenizations per ingest, on a worker thread
  under the shared write permit — until they carry marker rows. Per-ingest
  work then converges to ONE fingerprint (the ingested file's own text) plus
  bounded numpy comparisons. The same one-time wave recurs per vault after any
  future embedding-model change.
- **Backfill stamp is migration-time, not provenance.** Legacy non-fingerprint
  rows are stamped with the currently-configured model at migration time. A
  deployment that switched embedding models before upgrading keeps that cohort
  cross-model comparable (identical to pre-#697 behavior) until the next
  model change properly invalidates it.
- **256-dim residual.** A legacy 256-dim EMBEDDING centroid is
  indistinguishable from a text fingerprint (the identity column did not
  exist when it was written) and is treated as one — advisory-only impact,
  bounded by the near-dup threshold.
- **Centroid replacement on re-fingerprint.** A scanned non-comparable
  embedding row is permanently rewritten in fingerprint form (one row per
  file by `UNIQUE(file_id)`): it keeps its advisory group membership and
  stays reachable by the text path, but cannot rejoin embedding comparison
  if the model later switches back. This is the one-row-per-file shape
  required for scan-marking without a second advisory table.
- Advisory-only guarantees are unchanged: near-duplicate grouping never
  blocks or fails an ingest, touches only `document_near_dups`, and the
  enrichment precedence (file > vault > global) is unchanged on the healthy
  path.
