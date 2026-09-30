# Workstream B PR 5: migrate_embeddings.py CLI safety (Issue #694)

## What changed

### Operator scripts

- **`scripts/migrate_embeddings.py`** — the embedding-dimension migration CLI
  now actually delivers its documented safety claims:
  - **Crash-safe write ordering (T1-29-S-01):** the SQLite status reset now
    runs BEFORE the LanceDB wipe. A failure between the two (locked
    database, crash, Ctrl-C) leaves the old vectors intact as the rollback
    source instead of stranding `status='indexed'` rows over an empty
    index. A re-run against that leftover state (present-but-empty index
    with rows still claiming vectors) detects and repairs it — resetting
    the rows for re-embedding instead of reporting "Nothing to migrate" —
    instead of the old blind early return.
  - **`partial` files are reset too (T1-29-S-02):** the reset predicate is
    widened from `status='indexed'` to `status IN ('indexed','partial')`
    (the `partial` status exists since issue #513), and the "will re-embed
    all N files" accounting reflects it.
  - **Recovery-sweep compatibility (S06-SK2-03):** the reset restores
    `phase='queued'` (plus `partial_embeddings=0`, `chunks_failed=0`,
    `error_message=NULL`) on schemas that have those columns, so the
    legacy non-lease startup/periodic sweep
    (`status='pending' AND phase='queued'`) re-adopts the rows — with
    lease mode (the default) already matching on status alone. Ancient
    schemas without the columns still reset cleanly (PRAGMA-guarded SET).
  - **Settings failure refuses instead of wiping (T1-29-KR-02):** a
    settings-import failure with a readable stored dimension now refuses
    with exit code 4 unless `--force` is passed, instead of falling through
    to "Could not detect stored dimension. Proceeding with migration." and
    destroying even a dimension-matching index.
  - **Concurrent-writer refusal (T1-29-S2-04):** before the first
    destructive step the script probes for an active SQLite writer
    (short-lived `BEGIN IMMEDIATE`); a held lock refuses with exit code 5
    and operator guidance to stop the application first.
  - **`--help` under cp1252 (S06-SK2-06):** the U+2192 arrow in the
    argparse description is replaced with ASCII `->`, so piped/cp1252
    consoles can read the flags (exit 0).

### Documentation

- **`README.md`** — the Harrier-migration operator sequence now stops the
  application before running the script, describes the reset-then-wipe
  order, documents the interrupted-run recovery order (re-run the script
  BEFORE restarting the app) and the refusal exit codes, and the
  "safe to run multiple times" note explains the repair behavior.

### Tests

- **`backend/tests/test_b05_migrate_embeddings_safety.py`** (new, frozen
  issue-tracer acceptance checks C1-C7): reset-failure consistency, re-run
  repair, partial-row reset, settings-failure no-wipe, concurrent-writer
  no-wipe, legacy-sweep predicate match, and cp1252 `--help`.
- **`backend/tests/test_migrate_embeddings_cli_refusals.py`** (new):
  main()-level pins for exit codes 4 and 5 (index intact in both) and the
  repair-mode dry run.
- Existing `backend/tests/test_migrate_embeddings_dim.py` checks (matching
  dimension no-op, unknown-dimension refusal — issue #512 VECTOR-006) stay
  green unchanged.

## Notes

- The known sibling scripts `scripts/reset_embeddings.py` and
  `scripts/migrate_memories.py` carry the same defect classes and are owned
  by issue #705 ([Workstream B] PR 16).
- Exit codes: 3 = stored dimension undetectable on a non-empty index;
  4 = configured dimension unreadable; 5 = concurrent writer holds the
  database, or the database is unreadable/corrupt at the writer probe;
  6 = the SQLite database is unreadable/corrupt during repair detection.
  Each refusal names its cause and the `--force` / restore-the-backup
  remedy.
