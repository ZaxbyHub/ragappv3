# Vault access coherence: single-admin shares, shared effective-admin invariant, org adoption (Issues #1025, #1003)

## What changed

### Single-admin share writes are FK-safe (`backend/app/api/deps.py`)

- `resolve_granted_by` maps a principal to an FK-safe `granted_by` value: the
  single-admin sentinel principal (`users_enabled=False`, id 0, no `users`
  row) records NULL provenance on every share-granting write instead of
  tripping the `users(id)` foreign key and surfacing as a 500 (or a
  misleading duplicate 409). The guard is a strict int type check — only a
  genuine non-zero int id is bound — so non-int principal ids (e.g. service
  accounts' `"sa:<id>"` strings) also map to NULL. All six sharing-write
  sites (member add/patch, group-access POST/PATCH, vault-side bulk replace,
  group-side replace) bind it.

### Shared effective-admin invariant (`vault_members.py`, `vaults.py`, `groups.py`, `users.py`)

- A shared inventory (`authz_policy.direct_vault_admin_ids` +
  `group_derived_vault_admin_ids`, mirroring the policy evaluator's joins
  exactly) backs every last-admin guard, evaluated inside the writer's
  `BEGIN IMMEDIATE` transaction so concurrent mutations cannot both observe
  "still covered":
  - member demote/remove (`PATCH`/`DELETE /api/vaults/{id}/members/{uid}`):
    **409** "Cannot demote/remove the last effective admin";
  - group-access demote/revoke and vault-side bulk replace
    (`PUT /api/vaults/{id}/groups`): **409** on a clear/replace that would
    leave the vault with zero effective admins (refusal only when the vault
    HAD effective admins — an already-broken vault stays repairable);
  - group-side replace (`PUT /api/groups/{id}/vaults`): **409** naming the
    vaults, whole replacement rolled back;
  - user deletion (`DELETE /api/users/{id}`): **400** naming vaults where
    the target is the last effective admin (the cascade deletes their
    direct and group-derived coverage together).
- Coverage counts direct grants AND group-derived admin (org-validated group
  grants), so a demote/remove is allowed whenever any other effective admin
  remains — including the same user's own group grant (dual-admin demote).

### Organization adoption and coherence (`backend/app/api/routes/vaults.py`)

- `PUT /api/vaults/{id}` accepts `org_id` to adopt an **org-less** vault into
  an organization (NULL→org only). Refusals: **400** target org not found,
  org-to-org reassignment, or a foreign-org group-grant conflict (named
  group ids); **403** when a non-superadmin caller is not a member of the
  target org. Validation and the UPDATE share one `BEGIN IMMEDIATE`
  transaction, so a racing grant write (or racing adoption) cannot
  interleave; grant writers re-read the vault org the same way, so a
  committed adoption always flips a racing grant's outcome.
- Visibility `org` without an organization is now rejected (**400**) on both
  create and update: the row state `visibility='org' AND org_id IS NULL`
  matches no branch of any access query and would silently lock the vault.
- New audit event **`vault.org_id_updated`** on every successful adoption
  (metadata: `vault_id`, `old_org_id`, `new_org_id`, `old_visibility`,
  `new_visibility` — read in-transaction, never a stale snapshot), recorded
  after the adoption commit; refused adoptions record no event.

### Frontend

- The vaults Edit dialog offers an **organization picker** for org-less
  vaults only (org→org reassignment is out of scope); the selection resets
  on every dialog open so a canceled pick never leaks into another vault's
  edit, and `org_id` joins the PUT payload only when an organization was
  actually selected.

## Why

Share-granting writes answered 500 in single-admin mode (sentinel id 0
tripped the `granted_by` FK); the last-admin guards counted only direct
member rows, so removing a sole admin whose coverage actually flowed from a
group grant was refused (or, on user deletion, could strand a vault); and an
org-less vault set to `visibility='org'` — or one that could never join an
organization from the UI — fell through every access-query branch with no
error surface.

## Migration steps

- No schema, config, or dependency changes. New refusal codes replace former
  500s (single-admin shares) and former silent strandings (last effective
  admin, org-less `visibility='org'`). Callers that retried the 500s should
  treat 400/409 as terminal repair signals: add a vault member (or
  reactivate the account / revoke the conflicting grants) and retry.

## Known caveats

- The effective-admin inventory counts GRANT ROWS ONLY: it does not model
  the superadmin role baseline nor `users.is_active`, so guards can refuse
  conservatively (fail-closed) when sole coverage is a superadmin or a
  deactivated account; adding a vault member or reactivating the account
  remains the repair path.
- Adoption is NULL→org only by design; an adopted vault cannot be
  reassigned or returned to org-less, and the Edit dialog's org picker
  disappears once a vault has an organization.
