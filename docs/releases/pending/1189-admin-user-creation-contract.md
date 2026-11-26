# Admin-user creation contract: password entered twice + first-sign-in change statement (#1189 AC1)

Issue: #1189 ([Workstream O] PR 3 of 7 — admin-user creation contract).
Provenance: #1167 AC4 — the add-user dialog had one password field and no
first-sign-in statement.

## What changed

`frontend/src/pages/AdminUsersPage/CreateUserDialog.tsx` now implements the
admin-user creation contract:

- The password must be entered twice. A required `Confirm Password` field
  (`id="create-confirm-password"`, `type="password"`, native `required`
  validation) is rendered under the password field, and `handleSubmit` gates
  on `password === confirmPassword` with a `Passwords do not match`
  `toast.error` before any API call. The confirm field resets alongside the
  other fields on success and on dialog close.
- The dialog states that the password must be changed at first sign-in: a
  `text-sm text-muted-foreground` note ("The user must change this password
  at their first sign-in.", `id="first-signin-note") sits under the
  PasswordRequirements help text. Both password inputs reference it via
  `aria-describedby` (the primary input lists
  `password-requirements first-signin-note`, the confirm input uses
  `first-signin-note`).

Two existing suites that drive the create form were updated to fill the new
confirm field (behavior-reflecting, not test-weakening):
`frontend/src/pages/AdminUsersPage.error-handling.test.tsx` and
`frontend/src/pages/AdminUsersPage.error-handling.master-preserving.issue775.test.tsx`
(both `handleCreateUser` tests in each).

New regression spec:
`frontend/src/pages/AdminUsersPage/create-user-dialog.issue1189.test.tsx`
(4 tests — two password fields present + required, the first-sign-in
statement rendered and wired to both inputs via `aria-describedby`, mismatch
blocks `onCreate`, empty confirm blocks submission natively, matching
entries call `onCreate`).

## Why

#1167 AC4 (verified at filing revision `5eafe2504fe1b8e6b83208d60f53f49e29e82b0b`):
the dialog had one field and no statement on a deployed build. The copy is
not aspirational: the backend already enforces it. `POST /users/`
(`backend/app/api/routes/users.py`) inserts `must_change_password=1`, and
`backend/app/api/deps.py` restricts flagged users to the explicit
password-change path until they change it (login responses surface
`must_change_password`; the reset endpoint also sets it). Issue #1189 asks
the UI to state the observed auth policy — the frontend copy and the backend
policy agree, so no mismatch decision is required and no backend change was
made (backend changes are explicitly out of scope for this issue).

## Migration steps

None. Frontend-only change; no API, schema, or config change. Admins who
create users now type the password twice — existing single-field muscle
memory will hit the mismatch toast until the second field is filled.

## Known caveats

- The confirm gate is client-side only; the backend `POST /users/` contract
  is unchanged (single `password` field, `must_change_password=1` on insert).
  A client that bypasses the dialog behaves exactly as before.
- The first-sign-in statement is static dialog copy, not a live probe of the
  backend flag; if the backend ever stops forcing `must_change_password=1`
  on creation, the copy must be revisited (issue #1189 scopes backend
  changes out).
- Password strength rules and their help text
  (`frontend/src/components/shared/PasswordRequirements.tsx`) are untouched;
  the confirm field validates only equality with the primary field.
