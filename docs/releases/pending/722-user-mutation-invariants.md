# users.py mutation invariants: audit trail, session revocation, superadmin guards, delete FK fix (#722)

## What changed

- **Audit events on the four missing mutations** (all via the existing
  tamper-evident `safe_record_security_event` helper; only new event types):
  `user.deleted` (DELETE /users/{id}, with a pre-delete role/username snapshot),
  `vault.visibility_updated` (PUT /vaults/{id} visibility-carrying updates, old +
  new values in metadata), `vault.deleted` (DELETE /vaults/{id}, recorded after
  the durable delete), and `vault.group_access_replaced` (PUT /vaults/{id}/groups
  on BOTH exits — replace and the empty-list clear-all, which is the full-revoke
  form). Admin-guide catalogue updated, plus a note that deactivation revokes
  sessions.
- **Deactivation revokes sessions** (PATCH /users/{id}/active with
  `is_active: false`): `user_sessions` rows are deleted in the same transaction
  as the `is_active` flip, so a refresh cookie issued before deactivation can
  never mint access tokens again — including after reactivation. Guard-rejected
  (last-superadmin) deactivations delete nothing. Missing-table tolerance
  mirrors the reset-password path for pre-migration DBs.
- **One superadmin-target rule, five writers**: new
  `assert_can_modify_target(actor_role, target_role)` (the #560
  `assert_can_assign_role` pattern applied to target modification) is invoked by
  `update_user` (before any field application — a body without `role` no longer
  slips past), `admin_reset_password`, `update_user_active`, and the membership
  writers `update_user_organizations` / `update_user_groups`. A non-superadmin
  actor gets 403 "Only superadmin can modify superadmin users" on a superadmin
  target everywhere; superadmin self-flows and admin-on-member flows unchanged.
- **Last-active-superadmin guard on `update_user`'s role branch**: demoting an
  active superadmin via PATCH /users/{id} now carries the same atomic
  `COUNT(...) > 1` predicate `/role` uses — the guard rides the single UPDATE's
  WHERE clause, so co-submitted fields are refused together with the demotion
  (400 "Cannot demote the last superadmin").
- **Truthful reset-password audit metadata**: `user.password_reset` records
  `sessions_revoked` from the actual DELETE outcome (false when the
  `user_sessions` table is absent and nothing ran).
- **delete_user of a historical org creator succeeds**: `organizations
  .created_by` is set to NULL in the same transaction before the user DELETE,
  fixing the FK 500 for a user who created an org they no longer own.
- **Sessions-list expiry fixed**: `GET /auth/sessions` compares `expires_at`
  against a bound aware-UTC ISO parameter (same format every writer stores)
  instead of SQLite's space-separated `datetime('now')` — earlier-today ISO
  expiries are no longer listed as active.
- **Login purges expired sessions**: a successful login deletes that user's
  already-expired `user_sessions` rows (strict `<`, mirroring the access-token
  denylist purge), without the row's own cookie being presented.
- **Admin UI mirrors the target policy**: `AdminUsersPage` hides
  Edit/Reset/Orgs/Groups actions and disables the active toggle for plain
  admins on superadmin rows; the role Select stays rendered-but-inert (only the
  current role offered, pinned by test).

## Why

Audit remediation #722 (Workstream D PR 4 of 8): four cross-cutting contracts
(audit trail, session lifecycle, modify-target guard, last-superadmin count
guard) were implemented per-route and missing on specific routes/field
combinations, plus a schema FK gap that 500'd historical-creator deletes.

## Migration / compatibility

- No schema changes. `test_user_org_roles.py`'s local fixture schema gained the
  production `organizations.created_by` column it had omitted (fixture parity,
  assertions unchanged).
- API changes are the issue's required semantics; the admin-guide rule ("a
  non-superadmin cannot modify a superadmin target at all") is now enforced by
  all five users.py writers listed above. (The org-side membership writers in
  organizations.py / groups.py are org-scoped surfaces without a superadmin
  target concept and are out of this rule's scope.)

## Known limitations

- The structural fix (rebuild `organizations` with `ON DELETE SET NULL`) is
  sequenced to the Workstream B schema lane per the issue's Dependencies; the
  same-transaction NULL-ing closes the reachable 500 now.
- A user's expired session rows are purged at that user's next login,
  deactivation, or password reset; for a user who is never mutated again they
  persist until account deletion (the login purge is per-user). Recorded as a
  sweep observation.
