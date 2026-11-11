# Operator embedding scripts preserve a rollback copy and refuse fail-open (#1094, #1080)

Issues: #1094 (migrate_embeddings identity guard fails open — overlapping-id
foreign DB wipes the live index; probe failures skip the check) and #1080
(reset_embeddings wipes the LanceDB index when the configured database has no
files table). Collated by explicit user instruction.

## What changed

- **`scripts/migrate_embeddings.py` — the wipe preserves the previous index
  (AC1, #1094).** `_wipe_lancedb` no longer `shutil.rmtree`s the LanceDB
  directory: `_preserve_as_rollback` renames it to a timestamped sibling
  (`<index-dir-name>.rollback-YYYYMMDD-HHMMSS`), the rollback path is logged
  BEFORE the
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
  before any wipe, mirroring the migrate guard's exit-8 database-shape
  taxonomy: missing file, directory-as-path, backup-named paths (the same
  name/parent tokens migrate refuses), lock-shaped or permission
  `OperationalError` while OPENING the database (stop-the-app /
  check-permissions guidance — a live writer already holding the database
  surfaces at the reset step as a locked error, with the index untouched),
  corruption, `DatabaseError` (restore guidance), no `files` table, and a
  `files` table without the core
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
- **Both scripts — feedback-round guard additions.** Reset's preflight also
  refuses backup-named database paths; both scripts refuse a
  symlinked/junctioned index path BEFORE the reset commits (the rename would
  move the link, not the index); an unreadable index directory is refused
  with the state-aware message instead of crashing with a raw
  `PermissionError` after the reset; and the migrate run summary states the
  replacement truthfully when the index directory is absent or already
  empty. Exit 8 is dual-use on reset (preflight refusals AND post-reset
  preservation failures); migrate's equivalent preservation failures exit 5.
  Rollback directories hold the previous index's full contents (including
  all document chunk text), are NOT covered by in-app document/vault
  deletion, and accumulate one full-size copy per run.
- **Docs.** Migrate module docstring and README: the "irreversible /
  mid-wipe / `--force` to finish deletion" language is replaced by the
  rename/recreate failure and recovery semantics; exit 5 now includes the
  probe-refusal cause; both scripts' docs state that rollback directories
  accumulate until removed manually (`cleanup_backups.py` does not manage
  them), that rebuilding temporarily needs space for both indexes, and that
  the rename does not make concurrent application writes safe (stop the app
  first).

## Tests

Frozen acceptance checks (arm's-length authored; anchor receipts published on both
issues — v1/v2 pre-implementation, superseded by v3 after the reviewer-sanctioned
wave-2 check amendment): `backend/tests/test_migrate_embeddings_rollback.py` (rollback
preservation with exact naming + preserved-content markers; probe-failure
refusal with a disjoint-DB exit-8 control leg) and
`backend/tests/test_reset_embeddings_refusal.py` (clean `SystemExit(8)`
refusals naming both paths, no sqlite traceback, no stray database file,
index survives). Supplementary `backend/tests/test_operator_wipe_rollback_hardening.py`
(33 legs at the feedback head; the feedback round added 13: backup-path,
link + dry-run-link, index-inspection guards, truthful summary, exhaustion,
re-run/rename-back recovery, permission arm, close-tracking stream,
non-sqlite-stream boundary, and the lance-query inner handler; the
implementation-review round added the reset-side same-second-collision and
recreation-failure twins): R3 recoverability, minimal-schema reset, core-column refusal,
preflight taxonomy (directory/corrupt/lock), D2 boundary legs (mid-stream
no-hit, `DatabaseError`, CLI exit 5, iterator closure, lance-side `None`
control), rename collision/rename-failure/recreation-failure for both
scripts, empty-directory policy, and the 4.2 source-contract guardrail (no
`shutil.rmtree` in either script; preflight ordered before the first
connection).
