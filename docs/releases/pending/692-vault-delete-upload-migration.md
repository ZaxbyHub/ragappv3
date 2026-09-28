# Vault delete reclaims disk; upload migration moves rows with bytes (#692)

## What changed

### Vault delete now reclaims uploaded bytes and the vault directory
- `DELETE /vaults/{id}` collects every file's stored path inside its
  transaction and, strictly after the relational commit and the existing
  vector reconciliation, unlinks each upload through the same
  root-containment helper single-document delete uses, then removes
  `data/vaults/{id}/` (per-child failures are logged, never fatal).
- A vault deleted while a document is mid-ingest can no longer leave orphaned
  vector chunks: both ingest write paths re-check the `files` row immediately
  before writing vectors, and a post-write compensation discards chunks that
  raced a delete in the pre-write → write window. The enrichment proxy write
  gained the same pre-write gate and post-write compensation.

### Upload migration is two-store consistent, both directions
- `migrate_uploads` now resolves each legacy file's destination from the row
  naming the exact stored path (previously: first `file_name` match, which a
  same-named row in another vault could hijack). Rows with drifted stored
  paths are skipped and listed in `result.failed` instead of being copied
  with no row to claim them.
- The migration never overwrites a pre-existing destination (a user upload
  already living at the target name is preserved and the file is skipped).
- A successful move updates `files.file_path` in the same logical operation;
  a failed row update reverts the bytes to the legacy path the row still
  names and never deletes the only surviving copy.
- `rollback_migration` moves `files.file_path` back with the bytes (including
  collision-renamed destinations). It remains an operator-invoked tool — run
  it with `PYTHONPATH=backend` from the repo root if a migration must be
  undone; rows follow their bytes in both directions.

### Startup migration behavior change (operators)
- The startup upload migration is now awaited to completion before the API
  accepts requests (the previous 15 s timeout could not actually stop the
  migration thread — requests were served mid-copy). First boot on a large
  legacy tree delays readiness by the real migration time; progress and
  elapsed time are logged. Container healthchecks are readiness-based and do
  not kill the process, but very large trees will hold the container
  "starting" until the migration finishes.

## Known limitations
- The AC6 structural guard (ninth Quality-contracts gate,
  `scripts/check_b03_upload_migration_timeout.py`) detects the contradicting
  `asyncio.wait_for(asyncio.to_thread(migrate_uploads ...))` shape via AST;
  it is fix-agnostic by design (correcting the startup claim instead of
  removing the timeout also passes).
- Disk cleanup after a failed unlink/rmtree remains log-only (parity with
  single-document delete); unlike vector chunks there is no durable disk
  tombstone yet.
