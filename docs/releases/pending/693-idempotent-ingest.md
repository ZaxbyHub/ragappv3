# Idempotent ingest: reprocessing an unchanged file stops appending duplicate vectors (#693)

## What changed

### Same-hash reprocess is a no-op, and legacy duplicates converge
- `VectorStore.add_chunks` accepts an optional `generation_prefix`
  (`{file_id}_{hash8}_`, the same string the safe-reupload cleanup derives).
  With it, the write is idempotent per generation: a COMPLETE same-generation
  re-add performs no writes at all, and any partial, duplicated, or stale
  state under the prefix — including databases already holding the
  duplicates this bug produced — is reset before the append so the file ends
  with exactly one row per chunk id. Both safe-reupload call sites
  (`process_file` and `process_existing_file`) pass the prefix; the guard
  counts against the selected table, so a dimension-rebuild reindex writes
  into its temp table unaffected by the live index's state.
- The repair branch has a transient zero-visibility window by construction
  (delete slice, then append); it only runs on already-anomalous states
  (partial/duplicated/stale generations) and self-heals on the next pass.
  Steady-state reprocessing never writes.

### FileWatcher's "already in DB" answer is path-form independent
- `_find_new_files` matches stored `file_path` rows with BOTH spellings of
  the scan directory (raw and resolved) and compares resolved forms, so a
  relative `DATA_DIR` no longer makes one stored form miss the LIKE filter
  and the other fail membership. Known limitation: rows stored in foreign
  equivalent spellings (mixed separators, `./` prefixes) still do not match
  either arm; both production writers produce one of the two canonical
  forms.

### Reconcile during a stop drain no longer strands the watcher
- `reconcile(auto_scan_enabled=True)` always schedules a loop-owned
  `_ensure_running` coroutine; if a `stop()` drain is under way it records a
  pending start that `stop()` honors after draining, so the watcher ends
  RUNNING instead of stopping while auto-scan is enabled. All lifecycle
  read-decide-write stretches execute on the watch loop.

### Scanned files pass the upload route's structural screens
- `scan_once` validates every new file through a new shared
  `upload_validation.validate_ingest_candidate` (magic bytes, image polyglot
  header screens scoped to image extensions, OOXML member check; extension
  case-folded exactly as the upload route does). Failures are logged and not
  enqueued; the bytes stay on disk for inspection and are re-screened on
  later scans. The route's PIL raster decode remains upload-only.

### `partial` documents own their content slot everywhere
- `_check_duplicate`, `_check_duplicate_in_flight`, and
  `_is_enrichment_job_current` all treat `status='partial'` like `indexed`
  for "this content already has a live document", so a re-upload or
  scan/email re-ingest of a partially-ingested file is rejected as a
  duplicate instead of creating a second document.
- The partial unique index `idx_files_hash_vault_indexed` is widened to
  `status IN ('indexed','partial')` via a staleness-gated, explicit-
  transaction swap in `run_migrations`: fresh databases get the widened
  index, already-widened databases skip the swap (no per-boot rebuild), and
  a legacy database holding an indexed+partial pair for one hash+vault rolls
  back to its previous narrow index with a warning. Note for rollback: a
  revert does not drop an already-widened index (the wider constraint is a
  superset of the narrow one).

### Path-based enqueue holds one job per path
- `BackgroundProcessor.enqueue` inserts path-based items (no `file_id`:
  email attachments, scanned files) through an atomic
  `INSERT … WHERE NOT EXISTS` keyed on a normalized `path_key`
  (`normcase(abspath)`), with the non-terminal set pinned to
  `('pending','running')` — a periodic scan (or a second email poll) while
  an attachment is queued-but-unstarted can no longer double-enqueue it.
  Known limitation: `normcase(abspath)` does not resolve junctions/symlinks
  (aliased deployments can still form distinct keys), and jobs rows created
  before this change carry no `path_key`, so one already-queued pre-upgrade
  job is not deduped (one-time transient).

## Notes for follow-on workstreams
- A same-dimension re-embed of identical content (Workstream B PR 6/#695,
  PR 7/#696) now sees a complete-generation no-op and will need to version
  or bypass the generation prefix deliberately — this PR is that work's
  declared dependency.
