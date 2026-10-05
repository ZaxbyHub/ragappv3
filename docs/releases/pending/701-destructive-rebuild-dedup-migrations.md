# Destructive rebuild and dedup migrations stop losing or duplicating rows (issue #701, Workstream B PR 12 of 16)

## What changed

- `migrate_add_wiki_relations_unique` no longer deletes rows the target
  `UNIQUE` index would have accepted. SQL `GROUP BY` folds NULL keys into
  one group, but SQLite `UNIQUE` treats NULLs as distinct — so a dedup key
  with ANY NULL column (`subject_entity_id` and `object_entity_id` are
  nullable; `predicate` is guarded too) never marks a row as a duplicate.
  Both the journaled doomed-count probe and the `DELETE` now restrict to
  rows with all three key columns non-NULL (issue #701, T1-05-K2-03).
- New helper `_preserve_autoincrement_high_water(conn, backup_table,
  canonical_table)` runs inside every rename-rebuild transaction before the
  backup-table `DROP`, carrying the backup's `sqlite_sequence` high-water
  mark onto the recreated table so ids of rows deleted before a rebuild are
  never reissued. Called at all 17 backup-drop sites: the 8 main-swap drops
  (curator `wiki_claims`, `wiki_claim_sources`, both `files` rebuilds,
  `document_reindex_jobs`, `wiki_lint_findings`, `draft_claims`,
  `draft_promotions`) and the 9 stale-backup recovery drops, which now
  repair an already-reset sequence instead of discarding the last record of
  the true high-water (issue #701, T1-06-S-02).
- `migrate_add_wiki_claims_normalized_text` backfills whenever rows still
  hold NULL, not only when the column is absent — the `ALTER TABLE ADD
  COLUMN` auto-commits outside the backfill's transaction, so a crash
  between the two used to leave an all-NULL column a re-run never repaired
  (the `chat_messages.seq` recovery pattern; issue #701, T1-05-S-03).
- `migrate_add_file_metadata_columns` backfills `files.modified_at` from
  `created_at` whenever rows still hold NULL (probe-first, so a converged
  boot performs no schema writes). The old column-absence gate was dead
  code under `run_migrations` (`init_db` adds the column first) and left
  every legacy row `NULL` forever; the ungated pass also repairs the
  already-damaged population and any crash window. When a backfill runs,
  the `files_search_fts` update trigger is dropped for the one-statement
  backfill and recreated from the identical DDL: `modified_at` is not an
  indexed column, so skipping the sync is a semantic no-op, while firing it
  on rows that predate the FTS index raises FTS5's "database disk image is
  malformed" (issue #701, T1-05-S-09).
- Three findings in the issue were already fixed on master before this PR
  and are pinned PRESERVING rather than re-fixed here: the
  `migrate_relax_draft_claims_span_not_null` legacy-path coverage now exists
  in `test_b10_journal_noise_guard.py` (#699 / PR #850, which also added the
  `wiki_relations.claim_id` remap), and `migrate_add_org_slug_column`'s
  every-invocation slug repair came from #690.

## Tests

- `backend/tests/test_b12_migration_coverage_guard.py` — static coverage
  guard for the legacy rename-rebuild path (AC1).
- `backend/tests/test_b12_dedup_migrations.py` — the six behavioral pins
  (AC2-AC7) plus the AC1 companion driving the full legacy swap
  (`legacy_alter_table=ON` child-FK target, nullable spans, row parity,
  clean `foreign_key_check`).
- `backend/tests/test_b12_migration_edges.py` — critic-requested edge pins:
  NULL-subject and identical-NULL-object twins survive the dedup; true
  non-NULL duplicates still collapse; zero-copied-rows rebuild keeps the
  high-water (helper INSERT branch); `modified_at` converges on re-run via
  both the direct migration call and a full boot; the
  `wiki_lint_findings` rebuild preserves its high-water (second behavioral
  site).
- `backend/tests/test_b12_rebuild_highwater_guardrail.py` — positional AST
  census: every qualifying backup-table `DROP` in `database.py` (backup
  suffix `_old`/`_legacy_fk` + renamed-to-backup in the same function) must
  have its own `_preserve_autoincrement_high_water(conn, <backup>, ...)`
  call in the interval between the previous qualifying drop of that name
  and the drop itself, RED on the pre-fix tree, mutation-probed (removing
  or misplacing ANY of the 17 call sites goes RED).

## Notes

- Databases that already ran the buggy dedup have lost their NULL-keyed
  `wiki_relations` twins — unrecoverable, and documented in the issue; this
  PR stops future loss.
- The previously-damaged `modified_at` population is repaired on the next
  boot; readers (e.g. `draft_evidence_freshness`) now see `created_at`
  where they previously saw NULL — the intended repair; no consumer
  depended on NULL (verified).
- Pre-existing, out of scope, disclosed: the `wiki_relations` dedup's
  doomed-count probe runs outside an explicit transaction (the sibling
  claims dedup counts inside `BEGIN IMMEDIATE`), so a concurrent writer
  could in principle make the journaled count diverge from the actual
  delete; migration boots are single-writer in practice.
- `database.py` carries no bandit findings, so the SAST gates
  (`run_bandit.py`, `check_sast_baseline.py`) pass with no baseline
  regeneration (verified pre-push).
