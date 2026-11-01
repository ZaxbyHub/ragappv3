# Effective-admin guards on the membership/cascade routes (#1028)

Issue: #1028 (follow-up from PR #1027 — the six route families that PR's scope
boundary excluded).

## What changed

Six membership/organization mutation routes can no longer silently drive a vault
to zero EFFECTIVE admins (direct `vault_members` admins UNION group-derived
admins under the shared `authz_policy` inventory PR #1027 installed). Each route
now runs candidate discovery, a pre-state read, the mutation, and an ACTUAL
post-state re-read inside one `BEGIN IMMEDIATE` transaction, and refuses with
409 naming the affected vault(s) — rolling the whole mutation back — when a
vault that HAD effective admins would be left with none:

- `PUT /api/groups/{id}/members` (membership replacement)
- `DELETE /api/groups/{id}` (deletion cascades the group's `vault_group_access`
  admin grants)
- `PUT /api/users/{id}/groups` (both the clear and the replace branches, superadmin
  and caller-org-scoped forms)
- `PUT /api/users/{id}/organizations` (org-membership replacement; dropping an
  org fails the inventory join's `org_members` EXISTS and the dropped-org cleanup
  removes `group_members` rows)
- `DELETE /api/organizations/{id}/members/{uid}`
- `DELETE /api/organizations/{id}` (deletion cascades groups → group_members and
  vault_group_access; vault rows survive with a dangling `org_id`)

The refusal fires only on non-empty-to-empty transitions: a vault that is
already at zero effective admins keeps today's behavior on all six routes, so
repair paths (adding a member, a group, or restoring an org membership) are
never deadlocked (issue AC7). Vaults whose coverage also survives through
another direct or group-derived admin are unaffected — the guards compare the
real post-mutation rows, not the candidate set.

The refusal message names the exact affected vault ids, e.g. `Deleting this
group would leave vault(s) [3] without an effective admin: each vault must keep
at least one effective admin`. Operators hitting the 409 should restore coverage
on the named vault BEFORE retrying: add a direct `vault_members` admin, or add a
covering group grant whose group has a member that also sits in the group's
organization (a bare grant with no qualifying member contributes nothing — the
inventory requires the org-membership premise). For the two deletion routes the
covering group must lie OUTSIDE the group/organization being deleted — the retry
itself would destroy a covering grant inside it. Restoring a dropped org
membership alone repairs only the stale-premise shape (the group row intact);
when the drop also deleted the group rows, re-add the group membership as well.

Scope notes: the two `authz_policy` inventory functions are untouched (the
guards are route-layer composition over them, per the issue's Out-scope) —
these six routes complete the guard set whose already-shipped half is PR
#1027's vault-member, vault-side group-access, and `DELETE /api/users/{id}`
guards. The seventh family proposed in the issue's evidence comment
(`update_user_active` / an `is_active` inventory filter) remains an open
maintainer scope decision tracked on PR #1029's review record and is not
changed here.

## Known caveats

The effective-admin inventory counts grant rows only: it does not model the
`superadmin` role baseline or `users.is_active` (identical to the #1025
release notes' Known caveats, since the guards compose that same inventory).
A vault whose sole counted coverage is a deactivated account still passes the
guards, and a vault whose only conceivable admin is a superadmin is never a
refusal candidate (superadmins keep full access regardless). Adding a vault
admin or reactivating the account remains the repair path.

## Tests

- `backend/tests/test_1027_followup_mutation_graph.py` — frozen acceptance
  checks C1-C7 (six DISCRIMINATING refusals + the AC7 repairability floor).
- `backend/tests/test_1027_followup_mutation_graph_support.py` — positive
  non-empty-to-non-empty legs, mixed multi-vault refusals with exact ids and
  atomic rollback for all six families, scoped non-superadmin branches (incl. a
  committing scoped-replace leg proving foreign-org memberships survive),
  genuine zero-to-non-empty repairs, candidate-bearing pre-broken floors for
  all six families, the sequential-writer/arriving-grant pins, and an
  ordering pin proving every one of the seven candidate-discovery call sites
  runs its discovery read inside the write transaction (an in-transaction
  spy), plus the unscoped-replace refusal and audit success-event controls.
