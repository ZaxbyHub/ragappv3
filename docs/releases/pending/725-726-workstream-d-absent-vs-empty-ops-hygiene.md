# 725/726 — Workstream D close-out: absent-vs-empty route semantics, vault-admin invariants, and operational hygiene

Collated remediation of the last two Workstream D audit issues (#725, #726; audit-2026-09-23). One PR per user instruction.

## Single-admin mode (vault routes)

- `GET /api/vaults/accessible` now lists vaults for the single-admin principal (id 0): only a **missing** id is treated as "no principal", matching the authz missing-id deny. Previously the falsy check swallowed the sentinel and returned `[]`.
- `POST /api/vaults` works in single-admin mode (201): the creator-membership insert is skipped for the id-0 sentinel (its `vault_members.user_id` FK has no `users` row), while the vault transaction itself always commits.

## Default vault identity

- Renaming the vault currently named **Default** away from that name, and deleting it, are refused with **409** and an actionable message. The startup migration, registration, and orphan backfill resolve the default vault by name; allowing rename/delete would silently mint a second "Default" vault (production incident in #265). Existing deployments with an already-duplicated Default vault are untouched.

## Replace-set bodies: omitted ≠ empty

- `PUT /api/vaults/{id}/groups`, `PUT /api/users/{id}/organizations`, and `PUT /api/groups/{id}/vaults` now reject a body with **every list field omitted (or null)** with **422** naming the fields. An **explicit empty list remains the only way to clear**. Previously `{}` deleted all rows.
- Duplicate ids in `PUT /api/vaults/{id}/groups` and `PUT /api/users/{id}/groups` are de-duplicated (first occurrence wins) instead of 500ing on the UNIQUE constraint.

## Paging

- `GET /api/users` validates `skip` (`ge=0`) and `limit` (`ge=1, le=1000`) — a negative limit (SQLite's unbounded sentinel) and a zero or over-cap limit are now 422. **Behavior change**: `limit>1000` is rejected rather than clamped; the shipped admin UI's page sizes top out at 100 (the API default), so first-party clients are unaffected. `GET /api/groups` now rejects `per_page<1` with 422 (previously an empty page) while keeping its existing default (100) and clamp (1000).
- `GET /api/vaults` and `GET /api/vaults/accessible` responses add `total` (exact authorized count) and `truncated` (true when the 1000-row cap dropped rows). The cap itself is unchanged but is now signaled, never silent. The TypeScript `VaultListResponse` gained the two optional fields.
- `GET /api/groups` validates `per_page` (`ge=1`) while preserving its existing default (100) and clamp (1000).

## Username and group validation parity

- Usernames (admin create/update **and** public registration) are rejected when **shorter than 3 characters after trimming** — `" a "` no longer slips through the raw length check. Values are stored exactly as sent (validation-only, no normalization; note: stored values are compared raw, so `" admin"` and `"admin"` remain distinct accounts, as before this change); registration keeps its clean 400 contract.
- Group name/description now enforce the UI's exact bounds on create and update: trimmed name ≥ 2, raw name ≤ 100, raw description ≤ 500 (previously 1-char names and unbounded text were accepted). Stored rows that already exceed these bounds must be shortened before they can be updated again.
- On the replace-set routes that accept both a canonical and a legacy field (`vault_access`/`group_ids`, `memberships`/`org_ids`, `vault_access`/`vault_ids`), sending both applies the canonical list and silently ignores the legacy field (pre-existing preference, now stated); duplicate group ids in one body keep the first occurrence's permission.

## Vault-member invariants and races

- `PATCH`/`DELETE /api/vaults/{id}/members/{uid}` gain a **last-admin guard** evaluated inside a `BEGIN IMMEDIATE` transaction (from the state read through the write): demoting or removing the user whose departure would leave the vault with zero **effective** admins (direct `vault_members` grants plus group-derived admin via `vault_group_access`) answers **409** ("Cannot demote/remove the last effective admin"); a mutation that leaves any other effective admin — direct or group-derived, including the same user's own group grant — is allowed. The guard is race-safe — two concurrent mutations cannot both observe "two admins".
- All four membership/group-access mutation handlers guard their post-commit re-reads and UPDATE rowcounts: a racing removal now answers **404**, never an unhandled `TypeError` 500, and never reports success for a mutation that did not apply. Same guards added to the user-update re-read and the revoke path. Note: a self-removal DELETE now answers **400** ("Cannot remove yourself") even when the caller is not a member of the vault — the self-guard precedes the member-existence check.
- The name **"Default" is reserved**: creating a vault with that name, or renaming another vault to it, is refused with **409** (completing the rename-away/delete refusals above — a squatted "Default" could otherwise never be repaired).

## Operational hygiene

- `create_vault`'s generic failure path logs the cause at ERROR (with the exception attached) before its fixed 500 — static message only, consistent with the C15 disclosure family.
- **`ALLOW_LOCAL_CURATOR` (and `ALLOW_LOCAL_SERVICES`) now honor `.env`-only configuration** (previously only a real process environment variable worked). Precedence: a key present in the process environment still wins at call time (dynamic flipping and compose behavior unchanged); otherwise the pydantic-settings `.env`-backed field applies. The enabled grammar is identical from both sources — only `1`/`true`/`True` (after trimming) enable; **any other value, from either source, leaves the SSRF guard armed** (`yes`/`on`/`TRUE` no longer enable via `.env`, and malformed values can no longer crash Settings at boot). **Posture note**: `.env.example` ships `ALLOW_LOCAL_SERVICES=1`, so an install that copies the template now runs with the local-services SSRF guard **disabled** by default — set the key to empty/`0` in `.env` to re-arm it. This also extends the #726 C11 curator scope to `ALLOW_LOCAL_SERVICES`.
- The admin guide documents that the bcrypt→Argon2id rehash is **one-way**: older bcrypt-only releases cannot verify upgraded hashes, and rollback requires the pre-upgrade database backup.
- `docs/engineering/testing.md` now names the real test seam `authz_policy._SQLITE_SERIALIZED` (patching `deps._SQLITE_SERIALIZED` never selected the branch), and a new behavioral parity test runs both branches of `get_effective_vault_permissions` and asserts identical merged permissions with the sequential branch proven to execute.

## Tests

- Frozen acceptance checks (24, arm's-length authored, checkpoint-anchored): `backend/tests/test_d07_absent_vs_empty.py`, `test_d07b_absent_vs_empty_hardening.py`, `test_d08_ops_hygiene.py`, plus the parity file `test_d08_sequential_branch_parity.py` and the sibling regressions `test_d08b_ops_hygiene_siblings.py`.
