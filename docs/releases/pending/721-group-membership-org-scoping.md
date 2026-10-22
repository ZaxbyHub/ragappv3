# Group membership now implies organization membership on every path (#721)

## What changed

- **Superadmin group assignment enforces the target-org check** (`PUT /users/{id}/groups`):
  the org-membership precondition of group membership now runs for every caller,
  including superadmins (previously 200, now 400 when the target is not a member of
  the group's organization). Superadmin caller-scope bypasses are unchanged.
- **Org removal revokes derived group memberships**: `DELETE /organizations/{org}/members/{user}`
  and `PUT /users/{id}/organizations` (which also now scopes its replace to the caller's
  own organizations, mirroring the groups route) delete the target's `group_members`
  rows for the affected organizations in the same transaction.
- **The vault-permission reader asserts the invariant**: `get_effective_vault_permissions`
  and the member-role vault listing resolve a group grant only while the user is a member
  of the group's organization — orphaned rows (from any legacy path or a raced write) no
  longer grant anything.
- **Group-member writes are race-free**: the org-membership check in
  `PUT /groups/{id}/members` moved inside the write transaction.
- **Grant provenance parity**: the group-side vault grant route
  (`PUT /groups/{id}/vaults`) and vault creation both record `granted_by`
  (the acting/creating user), identical to the other grant writers.
- **Invites survive renames and stop re-minting dead tokens**: renaming a user re-binds
  their own pending invites to the new username (still acceptable) and revokes pending
  invites already addressed to the new name (which would otherwise re-bind to the
  renaming user); `resend` now applies the exact redeemability checks `create` applies
  (email-form to nobody → 400; role dropped below member → 400; #300-style
  not-yet-provisioned username invites stay allowed).
- **One-off migration**: journaled startup purge of existing orphaned
  `group_members` rows (a user not in the group's organization), placed before the
  Default-vault orphan backfill. **Destructive**: purged rows are not recoverable from
  the journal (the count is journaled); idempotent — a converged boot is a no-op.
- **Docs**: admin-guide and engineering-conventions now state the
  group↔org-membership invariant, the reader-side predicate in the vault permission
  resolution order, and the explicitly-decided org-less-vault policy (a vault with
  NULL `org_id` accepts groups from any organization — behavior unchanged, now pinned
  by tests).

## Why

Audit remediation #721 (Workstream D PR 3 of 8): the group-membership/org-membership
invariant was re-implemented per route, missing on three of five writer/reader paths;
grant provenance and invite identity drifted on sibling paths.

## Migration / compatibility

- No schema changes. The purge migration is automatic at startup and journaled.
- API behavior changes are the issue's required semantics (superadmin cross-org
  assignment 400; scoped org replace keeps out-of-scope memberships; resend 400 for
  unredeemable invites; hijack-style renames revoke the stolen invite).
- Two test fixtures were corrected to seed valid chains (an org-less group member and
  an invite addressed to a nonexistent email-shaped username — states the API itself
  could never produce).

## Known limitations

- `org_invites` still binds invitees by username string at rest (the rename hook keeps
  it consistent); id-based binding plus the `organizations.created_by` ON DELETE
  rebuild are sequenced to the Workstream B schema lane per the issue.
