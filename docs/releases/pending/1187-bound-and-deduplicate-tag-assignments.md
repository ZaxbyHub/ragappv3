# Tag assignment batches are bounded and deduplicated (#1187)

Issue: #1187 (Workstream N, PR 6 of 7), consolidating #800 AC6 and AC12.

## What changed

`TagAssignRequest` (backend/app/api/routes/tags.py) now bounds both batch
axes that previously carried only `min_length=1`:

- `file_ids`: `max_length=1000`, aligned with `FolderMoveRequest`
  (backend/app/api/routes/folders.py), which uses the same documents
  per-page ceiling the UI selects against.
- `tag_ids`: `max_length=100`, a per-request bound (not a per-vault
  tag-count cap; tag creation stays uncapped) that keeps the files x tags
  INSERT loop at no more than 100,000 pairs per request.

Both ceilings stay far under SQLite's 32766 host-parameter limit, so the
vault-filtering `IN (?,...)` scans in `TagStore.assign_tags`
(backend/app/services/tag_store.py) can no longer be pushed past the
driver limit by a single request. Oversized batches are rejected with a
422 at the request-model boundary, before any database work: no partial
assignments can be persisted from a rejected batch.

Deduplication was already structural and is now pinned by tests:
`document_tags` has `PRIMARY KEY (file_id, tag_id)` and `assign_tags`
writes through `INSERT OR IGNORE`, so retried requests and duplicate ids
within one request collapse to the same persisted pair set.

## Why

A `POST /api/tags/assign` with 40,000 `file_ids` passed validation, then
reached `TagStore.assign_tags`, whose `_vault_file_ids` scan built one
`IN (?,...)` beyond SQLite's 32766 host-parameter limit, followed by
files x tags INSERTs on the event loop. Bound, reject, and keep the
idempotent path green.

## Migration steps

None for server or data. Clients that legitimately issue batches larger
than 1000 files or 100 tags per request must split them into multiple
requests and will now receive a 422 instead of a driver-level failure.
The documents list window is capped at 1000 rows
(`MAX_PAGE_SIZE` in frontend/src/components/documents/useDocumentPolling.ts),
but selections are preserved across filter views, and `assignTags` in
frontend/src/lib/api/tags.ts passes the UI's whole selection through
without splitting, so a selection accumulated across views can exceed
1000 files; it now gets a 422 (shown as the dialog's error toast), the
same behavior bulk folder move already has under `FolderMoveRequest`.

## Known caveats

- The event-loop INSERT loop in `TagStore.assign_tags` itself (files x
  tags sequential INSERTs inside one commit) is unchanged; only its
  input is bounded (worst case 1000 x 100 = 100,000 INSERTs). Moving that
  loop off the event loop is tracked by #1222.
- `DocumentTagsSetRequest.tag_ids` (the per-document PUT route) remains
  unbounded; it is out of scope for #1187 and tracked by #1222 (an
  oversized request there still reaches the host-parameter limit).
- The bulk-tag dialog (frontend/src/components/documents/BulkTagDialog.tsx)
  sends every checked tag in one request and does not split, so checking
  more than 100 tags in one bulk operation now returns a 422 that the
  dialog surfaces as its error toast; check fewer tags per operation. A
  friendlier client-side limit message is tracked by #1222.
- The limits are request-model constants, not configurable settings; a
  deployment needing a different ceiling requires a code change.
