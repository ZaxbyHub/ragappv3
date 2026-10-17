# fix(kms): vault recompile stops silently skipping partial-status files (#793)

## What changed

`POST /api/kms/recompile` is documented as recompiling "all document entries
in a vault", but the vault-wide selection behind it filtered
`files.status = 'indexed'` only. Files in status `partial` — the
content-available state the upload/reindex path writes when parsing succeeded
but at least one chunk failed (`chunks_failed > 0`) — hold retrievable
`parsed_text`, are compiled by the per-file ingest hook with no status check,
and own their `(file_hash, vault_id)` slot exactly like an indexed file
(issue #693's widened unique index). A vault recompile never created or
refreshed their KMS entries, and its `{compiled, skipped, total}` result
reported full success while excluding them from `total` entirely.

- **Eligibility aligned to the per-file path.**
  `KMSCompileProcessor._compile_vault` now selects
  `status IN ('indexed', 'partial')` — the same eligibility set the document
  retry/compile paths use — so partial files are compiled, refreshed, and
  counted in `total`. Genuinely ineligible statuses (`error`, `cancelled`,
  and in-flight `pending`/`processing`) remain excluded, and a selected file
  with empty `parsed_text` still skips per-file exactly as before (counted in
  `total` + `skipped`, never swept into an entry).
- **Existing partial files are backfilled by the next recompile.** Files that
  were never vault-compiled (including installs that ran with
  `kms_compile_on_ingest` disabled) gain their entries the next time
  `POST /api/kms/recompile` runs for their vault — no separate backfill step.
- **No other behavior changes.** The per-file compile path, the route, the
  enqueue contracts, and the job-result shape are untouched; only the
  vault-wide filter and its docstring changed. `total == compiled + skipped`
  continues to hold by construction.

New regression coverage: `backend/tests/test_o03_kms_compile_vault_status.py`
pins a mixed-status vault (indexed/partial/error, each with parsed text) —
entries exist for exactly the indexed + partial files and `total` counts both.
RED at base, GREEN with the fix; the `error` leg fails if the filter is
dropped entirely or `error` is admitted.
`backend/tests/test_o03b_negative_status_pins.py` pins the remaining
ineligible statuses (`cancelled`, `pending`, `processing` stay excluded) and
the empty-`parsed_text` skip (no entry, counted in `total` + `skipped`).
