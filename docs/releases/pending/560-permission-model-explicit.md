# Make the permission model explicit (Issue #560, Workstream J PR 1)

## What changed

### Backend — one role-assignment rule across every surface (C22)
- New shared helper `assert_can_assign_role(actor_role, target_role, new_role)`
  in `backend/app/api/routes/users.py`; `create_user`, `update_user`, and
  `update_user_role` all delegate to it, so the routes can no longer disagree
  about who may assign `users.role`.
- **Behavioral change (strict rule):** only a `superadmin` can grant
  `role=admin`/`role=superadmin` at creation (`POST /users/`) or change any
  existing user's role (`PATCH /users/{id}` with a *changed* role).
  Previously `PATCH /users/{id}` and `POST /users/` let any admin mint admin
  peers while `PATCH /users/{id}/role` required superadmin — the same actor
  and value got 200 from two routes and 403 from the third.
- No-op saves keep working: re-asserting a user's current role is treated as
  a no-op, so the Edit dialog (which always sends the role field) can still
  rename users. Admins can still create member/viewer accounts. A
  non-superadmin still cannot touch the role column of a superadmin target,
  even with a no-op value.
- The unreachable duplicate privilege-escalation guard in `update_user`
  (condition strictly implied by two earlier raises) was deleted rather than
  kept as a second layer.
- Frontend mirrors the rule via one helper:
  `frontend/src/pages/AdminUsersPage/roleOptions.ts` (`roleOptionsFor`),
  now used by `CreateUserDialog`, `EditUserDialog`, and the per-row role
  dropdown (which previously offered every role to every admin and called
  the superadmin-only `/role` endpoint).

### Backend — unresolvable org-invite identifiers rejected at creation (C24)
- `POST /api/organizations/{id}/invites` now rejects (400) an email-form
  identifier (contains `@`) that matches no existing username, because
  acceptance matches the invite identifier against the invitee's **username**
  and such a token could never be redeemed.
- Deliberately preserved: inviting a not-yet-provisioned user by
  **username-form** identifier (#300's future-user flow), inviting users
  whose username happens to be email-shaped, and the below-member rejection
  for existing viewers. Eight existing tests were updated to this contract
  with their intent preserved.

### Docs — documented + contract-tested vault permission matrix (C25)
- `docs/admin-guide.md` now documents the read/write/admin vault permission
  matrix (level hierarchy, actor baselines, per-operation minimum levels,
  and the deliberate chat asymmetry: viewing sessions needs `read`,
  creating sessions/sending messages needs `write`).
- `backend/tests/test_vault_matrix_doc.py` + `test_vault_matrix_contract.py`
  parse the documented table and assert each cell against the real
  `evaluate()` policy evaluator, so doc and code cannot drift apart.

### Tests — generic trailing-slash route-parity property (C19)
- The five hardcoded single-schema-entry methods in
  `backend/tests/test_route_parity.py` (four route groups) are replaced by
  the router-walking property test `test_route_parity_generic.py`: for every
  pair of routes differing only by a trailing slash, exactly one member is
  schema-visible, regardless of direction — across all routers, with an
  in-module probe proving the walker catches an unguarded twin.

### Docs — README/admin-guide endpoint descriptions
- README's Users table now states the real role-change gates and lists
  `PATCH /api/users/{id}/role` separately.

## Tests
- New: `test_role_assignment_rule.py` (the single rule, behavioral),
  `test_role_rule_source_contract.py` + `test_permission_surface_source_contract.py`
  (source-level guardrails: helper delegation, no inline superadmin
  hand-filtering, invite resolution guard),
  `test_invite_identifier_resolution.py`, `test_vault_matrix_doc.py`,
  `test_vault_matrix_contract.py`, `test_route_parity_generic.py`, and
  `roleOptions.test.tsx` (frontend helper).

## Known limitations
- The matrix *table* is mechanically pinned against `evaluate()`; the
  surrounding prose (implication rule, baselines, resolution order) is
  documentation only and is not parsed by tests.
- A username-form identifier that resolves to no user can still mint an
  invite (the future-user provisioning flow from #300); such a token is
  redeemable only if a user later registers with that exact username.
- Rollback: revert the commit; no migrations or data changes.
