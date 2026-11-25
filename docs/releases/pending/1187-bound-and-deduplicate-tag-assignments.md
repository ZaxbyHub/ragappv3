# Tag assignment batches are bounded and deduplicated (#1187)

Issue: #1187 (Workstream N, PR 6 of 7), consolidating #800 AC6 and AC12.

## What changed

`TagAssignRequest` (backend/app/api/routes/tags.py) now bounds both batch
axes that previously carried only `min_length=1`:

- `file_ids`: `max_length=1000`, aligned with `FolderMoveRequest`
  (backend/app/api/routes/folders.py), which uses the same documents
  per-page ceiling the UI selects against.
- `tag_ids`: `max_length=100`, matching the size of the vault-wide tag
  palette the routes manage.

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
than 1000 files or 100 tags per request (none known — the frontend
selects at most one documents page, and `assignTags` in
frontend/src/lib/api/tags.ts passes the UI's selection through) must
split them into multiple requests and will now receive a 422 instead of
a driver-level failure.

## Known caveats

- The event-loop INSERT loop in `TagStore.assign_tags` itself (files x
  tags sequential INSERTs inside one commit) is unchanged; only its
  input is bounded. Offloading that loop is a separate concern tracked
  by #800's other criteria.
- `DocumentTagsSetRequest.tag_ids` (the per-document PUT route) remains
  unbounded; it is out of scope for #1187 and its batches are naturally
  small (one document's tag set).
- The limits are request-model constants, not configurable settings; a
  deployment needing a different ceiling requires a code change.
