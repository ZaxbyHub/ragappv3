# Route-guard and org-integrity residuals: stale-caller re-validation, drained recovery/commit bookkeeping, one shared recovery-exemption predicate

Issues: #1043, #1058
PR: (filled at publication)

## What changed

### Six org routes re-validate caller privilege inside the write transaction (#1043)

`add_org_member`, `create_org_invite`, `resend_org_invite`, `revoke_org_invite`,
`set_prompt_override` and `clear_prompt_override` previously read the caller's
`_is_org_admin_or_owner` privilege on autocommit and then wrote with a bare
execute+commit (or via `PromptVersionStore`) — a caller demoted by a concurrent
committed transaction between the check and the write still landed their
mutation. Each route now keeps its outer fast-fail checks and adds one
`BEGIN IMMEDIATE` (`transaction_context`) worker that re-reads caller privilege
FIRST, then re-reads every mutable precondition (target identity, the
owner-assignment right, duplicate membership, invite state and redeemability),
performs the write, and reads the response row — refusing 403 on a stale
caller with everything rolled back atomically (the same pattern PR #1042
shipped on `update_org_member_role` / `remove_org_member` / `transfer_ownership`).
The superadmin bypass on the two prompt routes is preserved, and
`PromptVersionStore.set_org_override` / `clear_org_override` gained a
`commit: bool = True` keyword so the store write joins the caller's
transaction (`commit=False`) without duplicating its SQL; every other store
caller is unchanged.

### delete_vault recovery arms drain the whole rollback+restore sequence (#1043)

A cancellation delivered inside any of `delete_vault`'s three recovery arms
used to escape the arm before `restore_purge` ran, stranding the tombstoned
Draft Room bytes while the rows survived. All three arms now route through one
shared `_drained_vault_recovery` unit (rollback-then-restore, shielded via
`task_drain.drain_await`): a mid-unit cancellation is absorbed until the
restore completes and only then re-raised; an infrastructure failure of the
unit itself is logged with the returned error's traceback and the arm's
original exception still propagates; the `conn.in_transaction` gate (#723) is
evaluated before the unit starts, so a post-commit cancellation still does NOT
restore bytes that belong to deleted drafts.

### Commit boundaries no longer lose the security-audit row (#1043)

`drain_audit` protected the audit await, but the immediately preceding bare
`await asyncio.to_thread(conn.commit)` was an un-drained boundary: a
cancellation landing exactly there — after the commit work completed — unwound
the handler before the audit coroutine existed, losing the row for an applied
mutation. Two new `task_drain` helpers convert the sites named below (the wider worker-owned family is tracked in issue #1068):

- `drain_commit_audit(conn, audit_factory, *, after_commit=None)` — commit +
  optional post-commit callback (cache invalidation) + drained audit as ONE
  shielded unit; the four users.py tails (create_user, update_user,
  update_user_role, activation) were converted (delete_vault uses the
  second helper below, because its commit must stay structurally last so a
  commit failure still reaches the recovery arms);
- `drained_commit(conn)` — drains only the commit await for delete_vault,
  whose commit must stay last-in-try so a commit failure still falls into the
  recovery arms; an absorbed cancellation is deferred until after the
  mandatory rowcount-gated audit tail, before the best-effort vector
  reconciliation.

Commit failures re-raise exactly as before (no audit is created for a
mutation that did not apply).

### One shared recovery-exemption predicate (#1058)

The forced-password-change recovery exit is now decided by ONE method-aware
canonicalizing predicate, `is_recovery_exempt(method, path)` in the new
`app/security/recovery_routes.py`, consumed by BOTH the
`must_change_password` gate (`deps.py`) and the maintenance middleware's
write-block exemption. The two gates can no longer disagree: whatever the
recovery table names stays open through a maintenance window for exactly the
named methods. Maintenance's operational exemptions (admin toggle, token
refresh, logout) stay in the middleware as `_OPS_EXEMPT_POST_PATHS`, matched
at their mounted spellings exactly as before.

Behavior change (deliberate, #1058's method-aware policy): the predicate
answers POST-only for `/api/auth/change-password` and `/api/auth/login`, where
the old deps gate was method-agnostic. A flagged user issuing a wrong-method
request to those paths now gets `403 must_change_password` from the gate
instead of passing it and 405ing at the router — strictly tighter, and only
reachable for flagged users on wrong-method requests to the two recovery
paths. `GET /api/auth/me` stays the me-read exemption; every other method on
`/auth/me` stays blocked.

`backend/tests/test_exempt_paths_guard.py` was rescoped to the predicate
world: the classification guard is now method-aware over every mounted
(auth route × method) pair, the table is AST-extracted from
`recovery_routes.py` and cross-checked against the app's real mounted routes
(a router-local-only or dead entry fails loudly), and the #731 subset guard
is superseded by construction (maintenance consumes the same predicate; the
frozen wiring leg pins it).

### Tooling paths for the security package

`backend/app/security.py` became the `app/security/` package
(`__init__.py` + `recovery_routes.py`); every `from app.security import ...`
caller is unchanged. The non-import consumers were updated:
`backend/pyproject.toml` `[tool.mutmut] source_paths`,
`.github/workflows/nightly-quality-gates.yml` guard modules,
`scripts/check_pr_scope_drift.py`, and `docs/engineering/conventions.md`.

## Tests

- `tests/test_org_routes_caller_revalidation.py` — six real routes driven
  through the stale-caller window (RED at base, GREEN now).
- `tests/test_vault_delete_restore_drain.py` — cancellations at the rollback
  and restore awaits still restore (RED at base, GREEN now).
- `tests/test_commit_boundary_audit.py` — the deterministic
  commit-completes-then-cancel schedule still records the audit row.
- `tests/test_recovery_routes_guard.py` — the shared-predicate guard suite
  (three divergence mutants fail loudly; wiring leg).
- Family coverage: `test_task_drain_commit_helpers.py`,
  `test_commit_boundary_family.py` (per-site conversions + source census),
  `test_vault_recovery_arms_family.py` (all arms + post-commit gate +
  shared-helper census), `test_recovery_predicate_family.py` (full
  maintenance matrix, behavioral wiring of both consumers, prompt-store
  caller-owned transactions, superadmin/ordering pins), and the rescoped
  `test_exempt_paths_guard.py`.
