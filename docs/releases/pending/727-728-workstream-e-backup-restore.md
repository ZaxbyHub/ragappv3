# 727/728 — Workstream E: documented backup CLIs run, and restore is crash-consistent

Collated remediation of the first two Workstream E audit issues (#727, #728;
audit-2026-09-23). One PR per user instruction, matching the issues' adopted
"same release" co-fix constraint.

## Operator CLIs run as documented (#727, T1-28-K-01)

All seven documented operator entry points — `scripts/restore.py`,
`scripts/backup_set.py`, `scripts/backup_sqlite.py`,
`scripts/migrate_memories.py`, `scripts/reset_embeddings.py`,
`scripts/cleanup_backups.py`, and
`backend/scripts/vector_search_bypass_index_diagnostic.py` — now import
correctly when run from the repo root with no `PYTHONPATH` (each puts
`backend/` on `sys.path` beside the repo root, the pattern
`scripts/migrate_embeddings.py` already shipped). Previously every one of
them exited 1 at import with `ModuleNotFoundError: No module named 'app'`,
so no documented backup had ever been produced through the CLI. The scripts
still require the application's environment (the same `AES_KEY` /
`ADMIN_SECRET_TOKEN` / `JWT_SECRET_KEY` contract as the app itself).

## Backup sets match the default deployment (#727, T1-28-K-02)

- A deployment with **no draft-room directory** no longer records an
  unrestorable draft-room item; the item is skipped.
- A vault (or draft-room, or lancedb tree) that **exists but is empty** is
  recorded with an explicit `"empty": true` marker, and restore recreates
  the empty directory instead of rejecting the set. Legacy `/1` sets
  without the marker keep today's behavior.
- A vault directory that does not exist is skipped; `backup_set.main()`
  derives `<data_dir>/vaults` without invoking the mkdir-ing
  `settings.vaults_dir` property and checks the LanceDB directory BEFORE
  connecting (`lancedb.connect` would create it) — discovery no longer
  modifies the live tree.

## Re-runs never merge; tags never collide (#727, T1-28-S2-07 / S2-16)

- The set is built under a sibling `<output>.partial-<token>` staging
  directory and renamed into place only when complete. A **non-empty
  existing output directory is refused before anything is snapshotted or
  tagged** (an existing empty one — e.g. the admin guide wrapper's
  pre-created folder — is replaced).
- Tag names carry a per-run token (`backup-<timestamp>-<token>-<table>`),
  so two backups of the same table within one wall-clock second both
  complete. The same-second naming class is also fixed for the legacy
  `db_<ts>_k<ver>.enc` single-file backup and the `app_backup_<ts>.db`
  migration fallback name (both gain uniqueness tokens; the pruner's
  `db_*_k*.enc` glob still matches).
- If a run fails after tags were created, the tags it created are deleted;
  if that rollback itself fails, the staging directory is KEPT as evidence
  and the remaining live tags are named in the error log.

## Pruner manages sets and releases tags (#727, T1-28-K-04 / S2-04)

`scripts/cleanup_backups.py --directory <dir> --retention <days>` now
removes backup-set directories (any directory holding a valid
`backup_manifest.json`, aged by the manifest's mtime) in addition to the
legacy `db_*_k*.enc` files. Before removing a set it **releases the set's
live `backup-*` LanceDB tags** (resolved through `settings.lancedb_path`
at call time), so pruned sets stop pinning table versions and the store's
periodic `optimize()` keeps working past the retention window — previously
every live `optimize()` failed once the LanceDB cleanup window passed and
the app swallowed the error as a warning, silently stopping compaction.
Removal fails closed: an unreadable/unsupported manifest, a symlinked
candidate, or any non-absent tag-release error keeps the set for retry.
`--release-unreferenced-tags` (opt-in, default off because sets may live
in several directories) additionally deletes live `backup-*` tags no
remaining set manifest in that directory references, keyed `(table, tag)`;
its census is fail-closed too. Negative retention is rejected.

## Restore is all-or-nothing, WAL-safe, and serves the tagged generation (#728, T1-28-S-05 / S2-01 / S2-03 + ENH-01)

`restore_backup_set` verifies **every** manifest digest and the generation
binding in a private staging directory before anything is written, then
swaps the destination in one rename. Behavior changes:

- **All-or-nothing**: a failure at any single write/copy/rename step — or
  a LanceDB persistence failure — leaves the destination byte-identical
  to its pre-restore state or fully restored, never a mix (the audit's
  fault-injection spike measured 19/22 torn outcomes before). A discarded
  restored copy may remain as `<dest>.failed-*` (safe to delete).
- **Unmanaged content survives**: destination content the set does not
  cover (`documents/`, `uploads/`, uncovered vaults, ...) is copied
  through the swap; stale files inside covered trees do not resurrect.
  Symlinks are preserved as links; a junction inside an unmanaged tree
  refuses the restore pre-swap. **Restore replaces the covered parts of
  the destination wholesale** — previously only manifest items were
  replaced.
- **WAL-safe**: stale `app.db-wal`/`-shm`/`-journal` sidecars are never
  carried into the restored tree (they leave with the pre-restore
  directory when the set replaces the database), so a fresh SQLite open
  never replays the pre-restore database over the restored one; a set
  that does not cover `app.db` preserves the database together with its
  live sidecars (the journal of surviving bytes).
- **Tagged generations persist**: after the swap each manifest table is
  `checkout(tag)` + `restore()`-ed, so a FRESH LanceDB connection serves
  the tagged generation (previously the checkout ran on a discarded handle
  and fresh connections served newer, post-tag data).
- **Refusals**: restore refuses while another connection holds the
  destination `app.db` (the documented stop-the-app step is enforced),
  refuses a symlinked destination, refuses a backup set stored inside the
  destination, and refuses while a leftover `<dest>.restore-journal-*.json`
  from an interrupted restore exists (the journal's phase maps to the
  manual roll-forward/roll-back steps in docs/operations.md). An O_EXCL
  `<dest>.restore.lock` serializes concurrent restores.
- **Space**: staging holds the set plus the unmanaged copies; the
  requirement is estimated and logged, and insufficient space refuses
  before anything is written.

## Known limitations

- The two-rename swap has a crash window between the renames; it is
  covered by the crash journal + the documented manual recovery (no
  portable filesystem transaction exists). Single-fault failures roll
  back automatically.
- A two-fault outcome (the rollback ladder itself fails) retains the
  journal and lock and names every directory in the raised error; it is
  NOT claimed as a clean rollback.
- Old (pre-fix) restore cannot consume sets the fixed producer writes
  with `"empty": true` items — the old reader rejects any empty files
  map. Rolling this change back therefore requires re-taking backups.
- Restore now needs roughly 2x the set size plus the unmanaged content's
  size free on the destination's drive.
