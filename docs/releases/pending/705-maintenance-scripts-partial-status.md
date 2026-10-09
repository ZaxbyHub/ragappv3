# 705 — operator maintenance scripts honor the 'partial' status and safe write ordering

## What changed

- **reconcile_lancedb_sqlite.py (`--multi-scale-indexing-enabled auto`, T1-28-K-06):**
  the `auto` resolver now defaults to the app's shipped
  `multi_scale_indexing_enabled=True` (config.py) instead of an independently
  decided `False`. Under the normal operating environment (`backend/scripts`
  is absent from the container image, so the env var is unset),
  `--delete-stale-multiscale --confirm` no longer classifies every live
  multi-scale row of an indexed file as stale and deletes it. Explicit
  `--multi-scale-indexing-enabled true|false` is unchanged.
- **reconcile_lancedb_sqlite.py (status model, T1-28-S-09/S2-09):** the
  report's live-file set widens to `status IN ('indexed','partial')` — a
  partial-success file's vectors are live and searchable (issue #513) — and
  orphan deletion protects `pending`/`processing` rows as well, both at
  plan-build time and via a fresh SQLite re-check immediately before the
  delete (`cleanup_plan.recheck_dropped_orphan_ids` records what the re-check
  dropped). The re-check narrows the two-store race window to
  [recheck, delete]; cross-store atomicity is impossible for a CLI, so the
  residual window is disclosed, not claimed closed. The report stays a
  truthful snapshot: a protected-but-unindexed file still appears in
  `file_ids_present_in_lancedb_but_not_sqlite_indexed` — it is only the
  DELETE set that excludes it. When every orphan candidate is protected, the
  script exits 0 with an empty plan (no `--confirm` demanded).
- **reconcile_lancedb_sqlite.py (report scaling, T1-28-S-10):** `build_report`
  indexes the chunk list by `file_id` once instead of rescanning it per
  indexed file — 204 chunk-list iterations at 200 files/400 chunks become 5.
  All report outputs are byte-identical (vault-mismatch `row_count` still
  counts only rows whose vault is in the mismatched set).
- **scripts/reset_embeddings.py (T1-28-S-11/S2-13):** the SQLite status reset
  now runs BEFORE the LanceDB wipe (the ordering scripts/migrate_embeddings.py
  adopted for #694) — a failed reset (locked/corrupt DB) leaves the old
  vectors intact as the rollback source. The reset covers
  `IN ('indexed','partial','pending')` (healing rows the pre-fix script
  stranded at `pending` with no phase) and restores the full queue-entry
  signal (`phase='queued'`, plus `partial_embeddings`/`chunks_failed`/
  `error_message` when the columns exist), so the startup recovery sweep
  (`status='pending' AND phase='queued'`) actually re-adopts the reset rows —
  the script's printed auto-reprocess promise is true again, scoped to the
  reset count. A "stop the application first" warning was added to the
  docstring and printed header.
- **scripts/migrate_memories.py (T1-28-S2-14):** the pre-migration backup and
  the maintenance flag now precede `init_db()` — `--rollback` restores the
  true pre-migration schema instead of a post-init one. `system_flags` is
  bootstrapped first (the app's own `_SYSTEM_FLAGS_DDL` degraded-boot
  pattern) so `MaintenanceService` construction works on legacy databases
  that predate the table. The rollback branch closes the pool before the
  restore (no writes through pre-restore connections) and clears the
  `maintenance=1` a migrate-path backup carries, on a fresh post-restore
  connection (best-effort, loudly warned). The rollback path never enables
  maintenance, so a decrypt failure cannot strand it.
- **backend/app/utils/transaction.py (S06-SK-05):** removed. `db_transaction`
  had zero callers and its `finally` released the pooled connection
  unguarded — the "removal is an owner decision" note in the #645 pending
  fragment is amended to record #705 as that decision.

## Tests

- Frozen acceptance suite `backend/tests/test_b16_maintenance_scripts.py`
  (C1-C9, authored at arm's length pre-fix; PRESERVING C10-C12 over existing
  tests): all RED→GREEN / GREEN→GREEN.
- Follow-ups `backend/tests/test_b16b_maintenance_followups.py`: real-backup
  rollback clears the carried `maintenance=1`; stranded `pending`/phase-NULL
  rows are healed while `processing` rows stay with the app's own sweeps; a
  C8-class census guardrail (no operator script filters the files table by
  `status='indexed'` alone) demonstrated RED at base; and two
  implementation-review mutation-pin tests (live-set widening and the
  pre-delete re-check are each individually verified).
- `test_reconcile_lancedb_sqlite.py::test_delete_orphan_file_ids_...` amended
  to the new protection contract (the pending file's rows survive; report
  assertions unchanged) — pre-authorized by the issue.

## Known limitations

- The reconcile script's residual [recheck, delete] window: a file ingested
  into both stores in the instant between the final re-check and the LanceDB
  delete can still lose rows. Cross-store atomicity is not achievable from a
  CLI; the re-check plus the stop-the-app usage model bound the risk.
- `reset_embeddings.py` deliberately excludes `processing` rows from its
  reset (the app's own age-gated startup sweeps own them); it also does not
  probe for a live writer — the stop-the-app warning is the contract.
- No migration required; no schema changes.
