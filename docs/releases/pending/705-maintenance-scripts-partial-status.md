# 705 — operator maintenance scripts honor the 'partial' status and safe write ordering

**Issue:** #705 ([Workstream B] PR 16 of 16; findings T1-28-K-06, T1-28-S-09, T1-28-S2-09, T1-28-S-10, T1-28-S-11, T1-28-S2-13, T1-28-S2-14, S06-SK-05)

## What changed

- **reconcile_lancedb_sqlite.py (`--multi-scale-indexing-enabled auto`, T1-28-K-06):**
  the `auto` resolver now defaults to the app's shipped
  `multi_scale_indexing_enabled=True` (config.py) instead of an independently
  decided `False`, and its env-var token set matches pydantic-settings' bool
  coercion (`t`/`y` included), so `auto` agrees with the running app for
  every value the app accepts. Explicit `--multi-scale-indexing-enabled
  true|false` is unchanged. Note that `auto` reads the process environment
  only — a value set in `backend/.env` (which the app also loads) is
  invisible to the script; pass the flag explicitly in that setup.
- **reconcile_lancedb_sqlite.py (status model, T1-28-S-09/S2-09):** the
  report's live-file set widens to `status IN ('indexed','partial')` — a
  partial-success file's vectors are live and searchable (issue #513) — and
  orphan deletion protects `pending`/`processing` rows as well, both at
  plan-build time and via a fresh SQLite re-check immediately before the
  delete. The re-check narrows the read-then-act window; a file landing in
  the residual instant between that re-check and the LanceDB delete can
  still be affected (cross-store atomicity is impossible for a CLI). The
  report stays a truthful snapshot: a protected-but-unindexed file still
  appears in `file_ids_present_in_lancedb_but_not_sqlite_indexed` — only the
  DELETE set excludes it (the report's `*_indexed*` key names therefore
  count indexed+partial rows). `cleanup_plan` now always carries
  `plan_protected_orphan_ids`, `recheck_dropped_orphan_ids`, and
  `vault_delete_skipped_in_flight` so its shape is flag-independent. When
  every orphan candidate is protected, the script exits 0 with an empty
  plan (no `--confirm` demanded) and says so on stderr.
- **reconcile_lancedb_sqlite.py (vault deletes, review PRR-002):**
  `--delete-orphan-vault <id> --confirm` now refuses (skips with
  `vault_delete_skipped_in_flight: true`) when the vault still owns a
  `processing` (mid-ingest) file; the delete remains an explicit override
  for everything else in the vault, including `indexed`/`partial` rows.
- **reconcile_lancedb_sqlite.py (report scaling, T1-28-S-10):** `build_report`
  indexes the chunk list by `file_id` once instead of rescanning it per
  indexed file — 204 chunk-list iterations at 200 files/400 chunks become 5.
  All pre-existing report outputs are unchanged (vault-mismatch `row_count`
  still counts only rows whose vault is in the mismatched set).
- **reconcile_lancedb_sqlite.py (robustness, review A04/PRR-020):** the
  protection lookup chunks its IN-list (500 parameters per batch) so very
  large orphan sets cannot exceed SQLite's variable limit, uses a 30 s busy
  timeout, and fails closed (an unreadable database raises instead of
  reporting an empty protected set).
- **scripts/reset_embeddings.py (T1-28-S-11/S2-13):** the SQLite status reset
  now runs BEFORE the LanceDB wipe (the ordering scripts/migrate_embeddings.py
  adopted for #694) — a failed reset (locked/corrupt DB) leaves the old
  vectors intact as the rollback source. The reset covers
  `IN ('indexed','partial','pending')` (healing rows the pre-fix script
  stranded at `pending` with no phase) and restores the full queue-entry
  signal (`phase='queued'`, plus `partial_embeddings`/`chunks_failed`/
  `error_message` when the columns exist). Adds a stop-the-application-first
  warning, re-run guidance for a failed wipe, and the corrected compose
  service name.
- **scripts/migrate_memories.py (T1-28-S2-14):** the pre-migration backup and
  the maintenance flag now precede `init_db()`, so `--rollback` restores the
  pre-migration schema instead of a post-init one (the backup legitimately
  carries `system_flags` and a maintenance row — it is taken while
  maintenance is enabled). `system_flags` is bootstrapped first via the
  app's own `_SYSTEM_FLAGS_DDL` (the degraded-boot double-definition
  pattern), which is conditional DDL on legacy databases but adds no new
  schema. The rollback branch closes the pool before the restore, warns the
  operator to stop the application first, and the restore itself is now
  staged and atomic (`copyfile` + writable mode + `os.replace`) with the
  target's stale WAL sidecars (`-wal`/`-shm`/`-journal`) removed before the
  swap — stale WAL frames from a crashed writer or a running app can no
  longer resurrect pre-restore content over the restored database. The
  post-restore maintenance clear runs an integrity check and reports a
  non-zero exit when it or the check fails, instead of printing an
  unconditional success.
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
  rows are healed while `processing` rows stay with the app's sweeps; a
  C8-class census guardrail (no operator script filters the files table by
  `status='indexed'` alone, including double-quoted and single-element-IN
  spellings) demonstrated RED at base; implementation-review mutation pins
  for the live-set widening and the pre-delete re-check; and feedback-round
  pins for the env-token parity with pydantic, the wipe-empties-the-dir
  invariant, the `error`/`cancelled` reset boundary and reset column values,
  the all-protected exit-0 shape, mid-run status flips into protected
  statuses, the vault in-flight skip, and a rollback restore that survives
  crash-style WAL sidecars.
- `test_reconcile_lancedb_sqlite.py::test_delete_orphan_file_ids_...` amended
  to the new protection contract (the pending file's rows survive; report
  assertions unchanged) and renamed to match — pre-authorized by the issue.

## Known limitations

- The reconcile script's residual race window: a file ingested into both
  stores in the instant between the final re-check and the LanceDB delete
  can still lose rows. Cross-store atomicity is not achievable from a CLI;
  the re-check plus the stop-the-app usage model bound the risk.
- `reset_embeddings.py` deliberately excludes `processing` rows from its
  reset (the app's own age-gated startup sweeps own them) and also leaves
  terminal `error`/`cancelled` rows untouched — after a wipe, those rows
  keep claiming vectors they no longer have and need a per-file admin retry
  (or manual cleanup) rather than being re-enqueued automatically. It also
  does not probe for a live writer — the stop-the-app warning is the
  contract. Under the shipped lease-mode recovery, a file whose ingestion
  retry budget was exhausted by a previous run settles to `error` instead of
  being re-processed; the printed next-steps call this out (app-side root
  fix tracked in #872).
- `--delete-stale-multiscale --confirm` with an explicit `--multi-scale-
  indexing-enabled false` deletes non-default-scale rows table-wide by
  design (the pinned base contract): rows owned by files in any status are
  deleted when the operator explicitly declares multi-scale disabled.
- `migrate_memories.py --rollback` stages the restore and removes stale WAL
  sidecars, but a live application writing during the restore can still
  corrupt the result — the stop-the-app warning is part of the procedure.
- No migration is required and no new schema is introduced; on legacy
  databases that predate `system_flags`, the script creates that one table
  via the app's own idempotent DDL constant.

## Known limitations (pre-existing, unchanged here)

- `scripts/reset_embeddings.py` and the other operator scripts must be run
  with the repo checkout's environment (they rely on the app package being
  importable; `PYTHONPATH=backend` if run outside the documented layout).
- `scripts/migrate_memories.py` writes its backups under the
  working-directory-relative `backups/` path.
