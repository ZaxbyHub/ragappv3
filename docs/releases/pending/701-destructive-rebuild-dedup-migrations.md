# Destructive rebuild and dedup migrations stop losing or duplicating rows (issue #701, Workstream B PR 12 of 16)

## What changed

- `migrate_add_wiki_relations_unique` no longer deletes rows the target
  `UNIQUE` index would have accepted. SQL `GROUP BY` folds NULL keys into
  one group, but SQLite `UNIQUE` treats NULLs as distinct — so a dedup key
  with ANY NULL column (`subject_entity_id` and `object_entity_id` are
  nullable; the `predicate` guard is defensive, as the column is NOT NULL
  in every historical shape) never marks a row as a duplicate. Both the
  journaled doomed-count probe and the `DELETE` now restrict to rows with
  all three key columns non-NULL (issue #701, T1-05-K2-03).
- New helper `_preserve_autoincrement_high_water(conn, backup_table,
  canonical_table)` runs before every backup-table `DROP`, carrying the
  backup's `sqlite_sequence` high-water mark onto the recreated table so
  ids of rows deleted before a rebuild are never reissued. Called at all
  17 backup-drop sites: the 8 main-swap drops (curator `wiki_claims`,
  `wiki_claim_sources`, both `files` rebuilds, `document_reindex_jobs`,
  `wiki_lint_findings`, `draft_claims`, `draft_promotions`) and the 9
  stale-backup recovery drops, which now repair an already-reset sequence
  instead of discarding the last record of the true high-water (issue
  #701, T1-06-S-02). A corrupt `seq` value (non-integer, or outside the
  plausible AUTOINCREMENT range) is skipped rather than aborting the
  migration or copying an id-exhausting counter. The 9 restore branches
  (drop canonical + rename backup in) are untouched — the sequence row
  follows the rename.
- `migrate_add_wiki_claims_normalized_text` backfills whenever rows still
  hold NULL, not only when the column is absent — `init_db` adds the
  column ahead of this migration on the boot path, and a crash between any
  ALTER and its backfill used to leave an all-NULL column a re-run never
  repaired (the `chat_messages.seq` convergence pattern; issue #701,
  T1-05-S-03). The bulk UPDATE is bracketed by the
  `wiki_claims_fts_update` sync trigger (dropped before, recreated after,
  gated on the FTS table existing) and the FTS index is rebuilt, mirroring
  the files-side guard: firing the external-content 'delete' command on a
  row missing from the index raises FTS5's "database disk image is
  malformed". BLOB `claim_text` values decode instead of failing the
  backfill. The whole backfill runs in one explicit transaction.
- `migrate_add_file_metadata_columns` backfills `files.modified_at` from
  `created_at` whenever rows still hold NULL (probe-first, so a converged
  boot performs no schema writes; the read cost is one unindexed probe per
  boot, the same shape as the `chat_messages.seq` precedent). The old
  column-absence gate was dead code under `run_migrations` (`init_db`
  adds the column first) and left every legacy row `NULL` forever; the
  ungated pass also repairs the already-damaged population and any crash
  window. When a backfill runs, the `files_search_fts` update trigger is
  dropped and recreated from the identical DDL — `modified_at` is not an
  indexed column, so skipping the sync is a semantic no-op, while firing
  it on rows that predate the FTS index raises FTS5's "database disk
  image is malformed". The trigger recreate is gated on the FTS table and
  its indexed columns existing (a direct call on a minimal database
  leaves no dangling trigger), and the whole block runs in one explicit
  transaction so the trigger is never durably absent (issue #701,
  T1-05-S-09).
- The AST census guardrail for the high-water helper was hardened:
  each helper call's `canonical_table` literal is validated against the
  rename pairs actually present in the same function (a typo'd canonical
  — a silent no-op UPDATE — fails the census), and any `executescript`
  string that drops a backup-suffix table is banned outright.
  Non-literal SQL, module-level DDL, and dead-branch helper placement
  remain documented census limits in the test's docstring.
- The frozen coverage guard (C1) was amended (CHECK_WRONG) from a
  substring scan to AST-based call detection: a comment-only file no
  longer satisfies it, and its failure message now states exactly what
  the guard verifies. Fresh base+head replays GREEN; anchor receipt v2
  published (issue comment 6000407993; semantics digest unchanged).
- Three findings in the issue were already fixed on master before this PR
  and are pinned PRESERVING rather than re-fixed here: the
  `migrate_relax_draft_claims_span_not_null` legacy-path coverage now
  exists in `test_b10_journal_noise_guard.py` (#699 / PR #850, which also
  added the `wiki_relations.claim_id` remap), and
  `migrate_add_org_slug_column`'s every-invocation slug repair came from
  #690.

## Tests

- `backend/tests/test_b12_migration_coverage_guard.py` — static coverage
  guard for the legacy rename-rebuild path (AC1; amended to AST-based
  call detection with an honest failure message).
- `backend/tests/test_b12_dedup_migrations.py` — the six behavioral pins
  (AC2-AC7) plus the AC1 companion driving the full legacy swap
  (`legacy_alter_table=ON` child-FK target, nullable spans, row parity,
  clean `foreign_key_check`).
- `backend/tests/test_b12_migration_edges.py` — edge pins: NULL-subject
  and identical-NULL-object twins survive the dedup; true non-NULL
  duplicates still collapse; zero-copied-rows rebuild keeps the
  high-water (helper INSERT branch); `modified_at` converges on re-run
  via both the direct migration call and a full boot; the
  `wiki_lint_findings` rebuild preserves its high-water.
- `backend/tests/test_b12_feedback_fixes.py` — PR #856 review-response
  pins: the index-diverged `wiki_claims_fts` shape converges (heals the
  trigger + rebuilds the index) instead of raising; a direct files call
  without FTS leaves no dangling trigger and later UPDATEs work; corrupt
  `sqlite_sequence` values (text, int64-max) are skipped; BLOB
  `claim_text` decodes; and three draft_claims recovery branches are
  behaviorally pinned with high-water assertions — rename-only restore
  and authoritative restore against pre-NOT NULL legacy shapes, and the
  relaxed stale-drop (whose backup carries the authoritative counter;
  that branch operates on an already-relaxed canonical by necessity).
  The remaining disposition (relaxed restore) is covered by the #699
  recovery suite without a high-water assertion.
- `backend/tests/test_b12_rebuild_highwater_guardrail.py` — positional
  AST census: every qualifying backup-table `DROP` in `database.py`
  (backup suffix `_old`/`_legacy_fk` + renamed-to-backup in the same
  function) must have its own `_preserve_autoincrement_high_water(conn,
  <backup>, ...)` call in the interval between the previous qualifying
  drop of that name and the drop itself, with the `canonical_table`
  literal validated against the function's actual renames; RED on the
  pre-fix tree, mutation-probed (removing or misplacing ANY of the 17
  call sites goes RED).

## Notes

- Databases that already ran the buggy dedup have lost their NULL-keyed
  `wiki_relations` twins — unrecoverable, and documented in the issue;
  this PR stops future loss.
- The previously-damaged `modified_at` population is repaired on the next
  boot for rows that have `created_at`; rows without `created_at` stay
  NULL (the code excludes them deliberately — no fabricated timestamps).
  Readers (e.g. `draft_evidence_freshness`) now see `created_at` where
  they previously saw NULL — the intended repair; no consumer depends on
  NULL (verified).
- The `wiki_claims.normalized_text` backfill now also heals a drifted
  FTS index when it runs (trigger bracket + rebuild), closing the
  asymmetry with the files side.
- Pre-existing, out of scope, disclosed: the `wiki_relations` dedup's
  doomed-count probe runs outside an explicit transaction (the sibling
  claims dedup counts inside `BEGIN IMMEDIATE`), so a concurrent writer
  could in principle make the journaled count diverge from the actual
  delete; migration boots are single-writer in practice.
- `database.py` carries no bandit findings, so the SAST gates
  (`run_bandit.py`, `check_sast_baseline.py`) pass with no baseline
  regeneration (verified pre-push).
