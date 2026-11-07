# Operator embedding scripts preserve a rollback copy and refuse fail-open (#1094, #1080)

Issues: #1094 (migrate_embeddings identity guard fails open — overlapping-id
foreign DB wipes the live index; probe failures skip the check) and #1080
(reset_embeddings wipes the LanceDB index when the configured database has no
files table). Collated by explicit user instruction.

## What changed

- **`scripts/migrate_embeddings.py` — the wipe preserves the previous index
  (AC1, #1094).** `_wipe_lancedb` no longer `shutil.rmtree`s the LanceDB
  directory: `_preserve_as_rollback` renames it to a timestamped sibling
  (`lancedb.rollback-YYYYMMDD-HHMMSS`), the rollback path is logged BEFORE the
  live directory is recreated (so a recreation failure still names the
  recovery location) and printed in the run summary, and a same-second name
  collision appends `-1`..`-5` before refusing. An empty index directory is
  left as-is (no pointless rollback artifacts). A rename failure refuses with
  `MigrationRefused` whose message states the truthful state: the SQLite
  status reset has already committed, the index is intact — never "nothing
  changed". This is RECOVERABILITY, not identity detection: a foreign
  deployment's database whose autoincrement ids overlap the index's still
  passes the exit-8 heuristics, but the run now leaves a recoverable rollback
  copy instead of destroying the live index.
- **`scripts/migrate_embeddings.py` — probe failures refuse (AC2, #1094).**
  The `for batch in id_batches` loop moved out of the lance-scoped
  `except Exception` in `_index_shares_claimed_ids`: each lance query keeps
  its own narrow handler (lance-side unreadability still returns the
  documented `None`), while `sqlite3.Error` raised by the claimed-id stream —
  before the first fetch or mid-stream after no-hit batches — raises
  `MigrationRefused` (exit 5 via `main()`). The batch iterator is closed on
  every path. Pre-fix, a SQLite error was swallowed into `None` ("no
  evidence"), which the caller treated as a pass (executed probe M3b: a
  DISJOINT database that the working guard refuses completed the destructive
  path end to end when the stream raised).
- **`scripts/reset_embeddings.py` — preflight refusal taxonomy (AC1+AC2,
  #1080).** New `_assert_application_database` runs before
  `get_db_connection` (a bare `sqlite3.connect` CREATES a missing file) and
  before any wipe, mirroring the migrate guard's exit-8 taxonomy: missing
  file, directory-as-path, lock-shaped `OperationalError` (stop-the-app
  guidance), other open failures (permissions/corrupt header), `DatabaseError`
  (restore guidance), no `files` table, and a `files` table without the core
  `id`/`status` columns — each refused with `SystemExit(8)` naming both the
  database path and the LanceDB path. Executed Phase-2 probes FALSIFIED the
  issue's stated wipe mechanism at master (no-table/missing-DB legs crashed at
  the unguarded `UPDATE files` with a raw traceback BEFORE any wipe — the
  missing-file leg also materialized a stray empty database); the fix turns
  those crashes into clean refusals with no stray files.
- **`scripts/reset_embeddings.py` — guarded reset fragments (D3b).** Only
  `status` remains unconditional in the `UPDATE files` SET list; `chunk_count`
  / `processed_at` / `modified_at` join the already-guarded optional columns,
  so the preflight-accepted minimal `id`+`status` schema resets cleanly
  instead of crashing mid-UPDATE (mirrors migrate's batch-2 guards).
- **`scripts/reset_embeddings.py` — the reset wipe preserves a rollback copy
  (R3 disposition, executed probe).** The same rename-as-rollback replacement
  closes the real reset-side data-loss window the Phase-2 probes exposed: a
  schema-COMPATIBLE foreign database (wrong deployment, overlapping ids — the
  same shape as #1094's migrate case) reset its wrong rows and wiped the live
  index with "[3/3] Migration complete" at master; it now leaves a recoverable
  rollback sibling. Recoverability, not detection.
- **Docs.** Migrate module docstring and README: the "irreversible /
  mid-wipe / `--force` to finish deletion" language is replaced by the
  rename/recreate failure and recovery semantics; exit 5 now includes the
  probe-refusal cause; both scripts' docs state that rollback directories
  accumulate until removed manually (`cleanup_backups.py` does not manage
  them), that rebuilding temporarily needs space for both indexes, and that
  the rename does not make concurrent application writes safe (stop the app
  first).

## Tests

Frozen acceptance checks (arm's-length authored, anchor v2 published on both
issues): `backend/tests/test_migrate_embeddings_rollback.py` (rollback
preservation with exact naming + preserved-content markers; probe-failure
refusal with a disjoint-DB exit-8 control leg) and
`backend/tests/test_reset_embeddings_refusal.py` (clean `SystemExit(8)`
refusals naming both paths, no sqlite traceback, no stray database file,
index survives). Supplementary `backend/tests/test_operator_wipe_rollback_hardening.py`
(18 legs): R3 recoverability, minimal-schema reset, core-column refusal,
preflight taxonomy (directory/corrupt/lock), D2 boundary legs (mid-stream
no-hit, `DatabaseError`, CLI exit 5, iterator closure, lance-side `None`
control), rename collision/rename-failure/recreation-failure for both
scripts, empty-directory policy, and the 4.2 source-contract guardrail (no
`shutil.rmtree` in either script; preflight ordered before the first
connection).
