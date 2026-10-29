# 1036 + 1037: org-integrity race closure, audit-await cancellation hardening, brief-template identity scoping, delete-restore drain

## Summary

Collated closure of the two PR #1035 follow-up issues:

- **Organization-ownership races (#1036 item 3, #1037 items 1-2).**
  `transfer_ownership`, `update_org_member_role`, `remove_org_member` and
  `delete_user`'s worker now re-validate authorization and role state INSIDE
  their `BEGIN IMMEDIATE` transactions (the #1028/#1033 house pattern):
  a stale caller (demoted between the outer check and the write) is refused
  403 (transfer, role-update, removal), a concurrently removed transfer
  target answers 400, a vanished role-update target answers 404, and a
  zero-row promote or role update answers 409 and rolls back; the promote
  and role UPDATEs carry rowcount checks, and `update_org_member_role`'s
  UPDATE is predicated with `AND role != 'owner'`. Under client-driven
  operation, no interleaving with a concurrent `transfer_ownership` can
  leave an organization with zero owners or execute a demoted caller's
  mutation on these four routes.
- **Audit-await cancellation skips (#1036 item 1).** The drained-completion
  pattern PR #1035 shipped for `delete_user` is extracted to
  `app.services.task_drain` and applied at all 31 sibling
  `safe_record_security_event` sites (users/auth/draft_room/vaults): a
  cancellation delivered at the audit await no longer loses the audit row for
  a mutation that already committed — the audit completes through the
  cancellation, which then propagates.
- **Brief-template identity residual (#1036 item 2).** Draft Room brief
  templates move to principal-scoped localStorage keys
  (`ragapp.v1.draft-brief-templates:<principalId>`) with a per-principal
  session cache; the identity-switch reset wipes the namespace, and the
  legacy un-scoped key is never read (best-effort removal only). Residue on
  a degraded storage surface is scoped to its own principal, so the next
  principal can never read it. The assignment form principal-gates its local
  template list and apply affordance.
- **delete_user restore-path drain (#1036 item 4).** The recovery arms'
  rollback and `restore_purge` awaits are drained with cancellation deferred
  across the whole recovery sequence: a second cancellation delivered during
  recovery can no longer strand tombstoned Draft Room bytes; it propagates
  only after the restore completes.

## User-visible behavior changes

- The four org/user mutation routes can now answer 400/403/404/409 on
  interleavings that previously returned 200 while corrupting ownership state
  (delete_user's owner refusal and transfer_ownership's removed-new-owner
  refusal answer 400; update_org_member_role's vanished-target refusal answers
  404). Recovery from any pre-existing zero-owner organization is unchanged
  (superadmin `transfer_ownership`).
- Saved brief templates now live per account. Signing out clears them (they
  are local-only, and the app cannot assume the next sign-in is the same
  principal), so templates do not survive a logout/re-login cycle; they DO
  survive same-principal re-authentication without an intervening logout.
  Templates saved before this release (the legacy un-scoped namespace) are
  dropped at the first identity switch rather than migrated — the legacy
  payload has no owner information, and copying it could hand one user's
  templates to another.
- Client disconnects no longer silently skip security-audit rows for
  committed mutations (users/auth/draft-room/vaults).
- update_org_member_role no longer maps a raw sqlite busy/locked error to a
  409 (the previous route-local wrapper); such errors now propagate to the
  framework's server-error handler and surface as 500, while the retryable
  zero-row path still answers 409.

## Out of scope (disclosed)

- A cancellation delivered exactly at a mutation's `commit` await (with the
  detached commit thread winning the release race) can still skip the audit
  row; that commit-boundary window is timing-dependent in both directions
  and needs the heavier commit+audit-as-one-unit machinery.
- Six sibling organization routes still validate caller privilege only
  outside their write transactions (add_org_member, create/resend/revoke org
  invite, set/clear prompt override) — the same stale-caller authorization
  window this change closes on the four named routes.
- vaults.py `delete_vault`'s recovery arms still run their rollback/restore
  awaits un-drained (a different transaction shape; the fix needs re-derived
  interleavings).
- These three residuals are collated in the follow-up issue filed with this
  change (see the PR body).

## Test plan

- 6 frozen acceptance checks (all base-RED → head-GREEN):
  `test_audit_cancel_hardening.py`, `draftBriefTemplates.identityScope.test.ts`
  + `DraftAssignmentForm.identityScope.test.tsx`,
  `test_transfer_ownership_caller_race.py`, `test_users_delete_restore_drain.py`,
  `test_users_delete_owner_race.py`, `test_update_org_member_role_race.py`.
- Preserving suites green: the #1029/#1030/#1035 owner-race and
  delete-recovery suites, users/organizations/auth/draft_room route suites
  (the draft_room batch carries the 2 documented Windows-only
  `test_strips_traversal` failures — diff-foreign, reproduced in isolation);
  frontend preserving suites: `draftBriefTemplates.issue533.test.ts` (adapted
  to the principal arg) and the pre-existing Draft Room form suites (adapted
  to the `@/lib/api` mock contract the form's new auth-store import
  requires).
