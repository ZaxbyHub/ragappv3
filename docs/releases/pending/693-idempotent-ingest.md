# Idempotent ingest: reprocessing an unchanged file stops appending duplicate vectors (#693)

## What changed

### Same-hash reprocess rewrites in place, and legacy duplicates converge
- `VectorStore.add_chunks` accepts an optional `generation_prefix`
  (`{file_id}_{hash8}_`, the same string the safe-reupload cleanup derives).
  With it, the write is idempotent per generation AND content-correct: when
  the on-disk generation already holds exactly the incoming chunk ids (a
  same-hash reprocess that preserves the chunk layout), the rows are
  re-written IN PLACE via an id-keyed upsert — so a same-dimension re-embed
  (new embedding model, same chunk ids) updates the stored vectors instead
  of being silently skipped while the corpus is labelled with the new model
  (PR #828 review F-001) — and any state whose ids differ from the incoming
  chunks (a chunk-size change under the same hash), is partial, duplicated,
  or stale — including databases already holding the duplicates this bug
  produced — is reset before the append so the file ends with exactly one
  row per chunk id. Both safe-reupload call sites (`process_file` and
  `process_existing_file`) pass the prefix; the guard counts against the
  selected table, so a dimension-rebuild reindex writes into its temp table
  unaffected by the live index's state.
- The reset branch (incoming ids differ from the stored generation) has a
  transient zero-visibility window by construction (delete slice, then
  append) and self-heals on the next pass. A layout-preserving reprocess
  rewrites rows in place with no delete at all.

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
  RUNNING instead of stopping while auto-scan is enabled. All enabled-path
  lifecycle read-decide-write stretches execute on the watch loop; the
  disable arm keeps master's caller-thread read (unchanged behavior).

### Scanned files pass the upload route's structural screens

Known limitation: the scan gate mirrors the route's STRUCTURAL screens
(magic bytes, image polyglot headers, OOXML membership — empty files
reject, 1-7 byte text files pass, exactly like the route), but not the
route's `allowed_extensions` policy: an extension the route would refuse
at the allowlist (e.g. `.xyz`) is not refused at enqueue and is handled
downstream according to its detected content type — undetectable binary
payloads are rejected by the parser with a recorded error, while text-like
content may parse. Widening the allowlist to scan is a
behavior change no acceptance criterion pins, so it is left to the route.
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
- A same-dimension re-embed (Workstream B PR 6/#695, PR 7/#696) is handled
  by the id-keyed upsert: unchanged chunk ids are re-written in place, so
  the reindex job's re-embed lands new-model vectors without a manual prefix
  bypass. PRs that need to DISTINGUISH re-written generations (e.g. for
  cache invalidation) should version the generation prefix deliberately on
  top of this PR.
