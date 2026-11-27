# test(wiki-lint): close #934 coverage gaps — census RETURNING form, origin-preserving idempotence, cross-origin precedence

Follow-up C to the #791 fix (PR #930 review). Test-only change: no
production behavior is altered.

## What changed

- **Rollback census covers the RETURNING-style 0-row exit (#934 item 1).**
  `backend/tests/test_o01_c14_rollback_census.py` now also AST-detects
  names assigned from `X.execute('UPDATE ... RETURNING ...').fetchone()`
  and requires their no-row path to roll back before returning — both the
  `if row:` guard shape (else-branch or fallthrough is the 0-row path; the
  exact shape of `WikiStore.claim_next_pending_job` in
  `backend/app/services/wiki_store.py`) and the `if not row:` shape. The
  alternative rowcount spellings, taken-path rollback requirement, and
  every-store scan landed earlier with the pr930-20261015 PRR-016 round;
  self-tests pin each new form (fallthrough, else-branch, `not row`, an
  implicit `return None` falling off the function end, and a plain SELECT
  fetchone staying unflagged). Removing the rollback from
  `claim_next_pending_job`'s no-row path now fails the census, including
  when the trailing `return None` is removed with it. A rollback wrapped in
  `try:` / `with` (or in an enclosing `finally:`) on the no-row path counts
  as reached, so those shapes are not false-positives.
- **Migration idempotence asserts origin preservation, not just column
  presence (#934 item 2).** New
  `TestO01FreshInit::test_second_run_migrations_preserves_existing_origins`
  in `backend/tests/test_o01_migration.py` seeds rows of every origin
  (including a `curator` row that title-based re-derivation would flatten
  to `linter`), runs `run_migrations` twice plus a direct re-entry into
  `migrate_wiki_lint_findings_origin_auto_resolved`, and asserts every
  origin is unchanged. Deleting the `"CHECK (origin IN"` idempotence
  guard in `backend/app/models/database.py` now fails this test.
- **Migration journal outcomes are pinned (#934 item 3).** New
  `TestO01FreshInit::test_origin_migration_journals_start_and_succeeded`
  migrates an old-shape table and asserts `migration_journal` carries the
  `start`/`ok` and terminal `succeeded`/`ok` rows for
  `migrate_wiki_lint_findings_origin_auto_resolved`. The parity and
  foreign-key `RuntimeError` branches stay untested by design (the issue
  judges them effectively unreachable).
- **Reopen test pins the intermediate state (#934 item 4).**
  `test_auto_resolved_finding_reopens_on_recurrence` in
  `backend/tests/test_o01_wiki_lint_reconcile.py` now asserts the row is
  `auto_resolved` between the undetect and recurrence runs, and that
  reopen reuses the same row id (no per-flap duplicate row), in addition
  to the final open count.
- **Cross-origin precedence is pinned (#934 item 5).** New
  `TestUserTerminalSuppression::test_cross_origin_dismissal_suppresses_linter_redetect`
  in `backend/tests/test_o01_origin_guards.py` walks the exact issue
  scenario — linter detect, empty linter upsert (auto-resolve), curator
  upsert of the same fingerprint (rule 4 fresh row), user dismisses the
  curator row, linter re-detect — and asserts the finding stays
  suppressed and the curator row stays `dismissed`. Scoping rule 2 of
  `WikiStore.upsert_lint_findings` to same-origin rows now fails here (and
  only here). The rule-2/rule-3 reorder fails here too, and is also killed
  by the seeded-mixed test
  `test_seeded_mixed_state_suppresses_and_reordering_rule3_first_fails`;
  the single-row `test_mixed_auto_resolved_and_dismissed_stays_suppressed`
  pins neither.
- **Temp dirs are cleaned up (#934 item 6).** `backend/tests/test_o01_migration.py`,
  `test_o01_wiki_lint_reconcile.py`, `test_o01_origin_guards.py` and
  `test_o01_kms_update_entry_rollback.py` register
  `addCleanup(shutil.rmtree, ...)` for every `mkdtemp` (pattern from
  `backend/tests/test_schema_drift.py`).
- **Stale comment corrected, survival pinned (#934 item 7).**
  `backend/tests/test_wiki_linter.py`'s lifecycle comment now says
  `auto_resolved` (not `resolved`), and the test asserts the
  previously-open rows survive the resolve pass as `auto_resolved`
  alongside the still-`dismissed` target row.
- **Curator setup deduplicated (#934 item 8)** into the new shared helper
  `backend/tests/_wiki_lint_o01_support.py` (`make_env`, `CURATOR_FIELDS`,
  `DeterministicExtraction`, `CuratorHarness`), replacing the copies in
  `test_o01_wiki_lint_reconcile.py` and `test_o01_origin_guards.py`.
- **Fixture element used (#934 item 9).** `_seed_old_rows`'s fourth tuple
  element (expected origin) is now asserted in the backfill loop instead
  of a second hand-maintained title→origin list.

## Why

The #791 guard suite asserted weak observables: presence-only idempotence,
final-state-only reopen, a precedence probe that never mixed origins, and
a census blind to the RETURNING 0-row exit. Reviewer probes showed named
defects (idempotence-guard removal, origin-scoped suppression, missing
RETURNING rollback) left the suite green. Each item now has an assertion
that fails on the defect it names (verified by re-applying each mutation
locally: census flags the rollback removal, the origin-preservation test
fails without the idempotence guard, the cross-origin test fails under the
same-origin-scoped rule 2).

## Migration steps

None — test-only; no schema, config, or runtime changes.

## Known caveats

- The census descends into `try:` / `with` bodies on a RETURNING no-row
  path and treats the stores' transaction context managers
  (`with self.transaction():` / `_write_txn()`) and `finally:` rollbacks
  as finalizing every exit. Shapes it cannot read statically — a `raise`
  exit, loops, a guard nested under another `if`, a rollback under a
  condition, `if row is None` spellings — are deliberately not flagged (it
  prefers a possible false negative over flagging code it cannot prove
  dirty). The pre-existing rowcount and `if not row:` checks keep their
  taken-path granularity: a rollback that only a wrapping `try:` / `with`
  performs is not recognized there.
- The migration's `failed`-phase journal branches remain untested, per the
  issue's own reachability analysis (IntegrityError fires first; no
  REFERENCES constraint exists to violate).
