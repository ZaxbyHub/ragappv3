# 723: Vault deletion cleanup, vault-read cost, and crash-window durability

Issue: #723 ([Workstream D] PR 5 of 8 — audit findings T1-21-S-03, T1-21-S-07, R1-G5b-01, T1-21-S2-01..S2-04).

## What changed

- **Vault deletion now removes every vault-scoped table without an FK to
  `vaults`** (13 tables: the wiki content/job tables, `kms_entries`,
  `kms_compile_jobs`, `document_reindex_jobs`, `canvas_artifacts`), inside the
  same `BEGIN IMMEDIATE` transaction as the rest of the delete. Previously
  these rows survived the delete forever (`delete_vault`'s docstring promised
  "all associated data" while ~12 tables persisted). Job-table history rows
  (completed/failed/cancelled) are deleted with everything else — a deleted
  vault leaves no compile/reindex history behind. NULL-vault rows (e.g.
  global reindex jobs) are untouched.
- **Pending/running unified lease jobs for the vault are cancelled** in the
  same transaction (`status='cancelled'`, json_valid-guarded payload match —
  the shipped `KmsStore` lease-cancel shape). Worker dispatch paths (wiki,
  KMS, reindex) and the five compiler entry points now check that the job's
  vault still exists before writing, so a job pending or in flight at delete
  time can no longer re-persist wiki/KMS content for a dead `vault_id`. A
  NULL-vault (global) reindex is never gated by this check.
- **`GET /api/vaults`, `/vaults/accessible`, `/vaults/{id}` stop paying a
  file×memory×session cross product.** The counts query's three-way
  `LEFT JOIN` + `COUNT(DISTINCT)` (measured 911 VM-op ticks at 40 files /
  40 memories / 40 sessions; 1.75s at 1000/100/100) is replaced by three
  correlated scalar subqueries returning identical counts.
- **Vector tombstones are durable before COMMIT.** One
  `vector_delete_pending` row per deleted file is written inside the delete
  transaction (and removed again when the post-commit vector reconciliation
  succeeds), so a crash or `asyncio.CancelledError` between COMMIT and the
  vector-store delete can always re-drive cleanup — previously no durable
  record existed unless the vector delete itself raised a handled `Exception`.
- **Draft Room crash windows closed.** `tombstone()` refreshes the `.trash`
  entry's mtime (age now measures tombstone age, not upload age) and writes a
  `<token>.json` sidecar recording the owning relpath; startup reconcile now
  restores tombstoned bytes still owed to a live `draft_inputs` row (exact
  rule via the DB-derived relpath set) before the age purge. `delete_vault`
  and the four restore-armed scopes in `draft_deletion.py` gained
  `except BaseException` backstops — gated on the transaction still being
  open — so a `CancelledError`/`KeyboardInterrupt`/unlisted exception no
  longer strands a surviving draft's bytes in `.trash` (and never resurrects
  bytes whose rows were already deleted).
- **The vault-delete confirmation copy now tells the truth about memories.**
  The dialog said memories are "permanently deleted"; the route reassigns
  them to global memories. Product decision (recorded in the issue's AC3 and
  the PR): the route keeps reassigning (pinned by existing tests); the copy
  now says "Memories will be kept and moved to global memories."

## Known residual boundaries (disclosed)

- The storage-level `reconcile` API keeps a pairs-only call shape whose
  restore rule is coarser than the shipped startup path's exact relpath rule:
  a pair-granularity caller can restore a concurrently-deleted input's bytes.
  The production startup caller passes the exact DB-derived relpath set and
  is not affected.
- `.trash` tombstones created by pre-fix builds carry no sidecar; if such a
  tombstone's owning draft survived a crash-era rollback, the age purge still
  removes its bytes (no metadata exists to identify them; bounded to
  tombstones created before this change).
- The vault-existence checks narrow the dead-vault write race to inside a
  single compile; a compile already past its entry check when the vault
  delete commits can still land rows for the dead vault until the next
  delete or sweep (irreducible without write-path fencing).
- `users.py::delete_user` mirrors the old two-handler restore shape; it is
  out of this issue's scope and disclosed for a maintainer decision (see the
  PR body).

## Verification

- `backend/tests/test_d05_vault_delete_integrity.py` (10 frozen checks,
  issue-tracer red checkpoint at base 4323afbb) and
  `backend/tests/test_d05_vault_delete_integrity_guardrails.py` (17
  guardrail tests: worker guards incl. NULL-vault reindex, BaseException
  backstops, 15-surface cleanup census, sidecar semantics, and the two
  production startup-restore wiring tests — tuple order and exact-rule).
- Blast radius: vault/membership/C19-rollback/draft-room/wiki/kms/reindex/
  vector-tombstone suites pass locally; only the documented Windows-only
  environmental failures remain (gold-corpus CRLF hashes, symlink privilege,
  POSIX-only traversal).
