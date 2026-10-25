# Refuse the well-known default ADMIN_SECRET_TOKEN in single-admin mode (#1001, Item A)

Issue: #1001 (follow-up from PR #996; Item B — the #993 AC2 wording
decision — is a maintainer decision record, not a code change).

## What changed

`Settings.reject_insecure_defaults` (`backend/app/config.py`) now refuses
to construct `Settings` when `USERS_ENABLED=false` and `ADMIN_SECRET_TOKEN`
equals the well-known literal `admin-secret-token` (new module constant
`_INSECURE_DEFAULT_ADMIN_TOKEN`). The `ValueError` names the field, the
single-admin rationale, and the generation one-liner, mirroring the
message shape of the existing empty-token refusal directly above it.

Decisions implemented per the issue's default recommendations (endorsed by
the maintainer's investigation comment on #1001):

- **Exact-match refusal (AC2).** The comparison is `==` against the
  literal. It does not mirror `main.py`'s startup warning: the refusal
  compares trim-then-exact (see the adjacent bullet), while `main.py`'s
  warning predicate is untrimmed exact — and after this change that warning
  is only reachable with the literal still booting, i.e. under
  `USERS_ENABLED=true`. Case variants (e.g. `ADMIN-SECRET-TOKEN`) are not
  the published literal and keep booting exactly as before.
- **Whitespace-trimmed comparison (closing the padded-literal gap).** The
  comparison runs on `admin_secret_token.strip()`, matching what
  `derive_audit_key()` actually feeds the HMAC: it strips the token, so a
  whitespace-padded literal (`" admin-secret-token "`) would otherwise
  boot AND still derive the public key `b"admin-secret-token"`. Pinned by
  `test_users_disabled_with_whitespace_padded_default_token_raises`.
  Case variants are unaffected — trimming does not change case
  sensitivity.
- **`USERS_ENABLED=true` unchanged (AC3).** Under multi-user auth the
  token is the required bootstrap/first-admin credential and seeds
  `admin_token_scopes`, so only the existing empty-token refusal applies;
  the rationale is recorded in the validator comment.

Follow-up hardening from PR #1007 review (V-1007-4): `Settings` now sets
`hide_input_in_errors=True`, so boot-validation errors no longer include
the input-value repr — co-located env values (e.g. a `JWT_SECRET_KEY`
passed alongside the refused token) are not echoed into startup logs.

Tests: raise/case-variant/users-enabled pins added to
`backend/tests/test_config_validators.py::TestRejectInsecureDefaults`,
plus the boot-error echo-suppression pin
(`test_boot_error_suppresses_input_repr_echoing_env_values`);
`backend/tests/test_deps_auth.py::test_get_current_user_accepts_explicit_default_token`
(which pinned the now-forbidden deployment shape returning 200) is amended
to `test_get_current_user_explicit_default_token_refused_at_config`,
pinning the config-layer refusal instead. The defense-in-depth guard in
`backend/app/api/deps.py` (rejects the literal only when no token is
configured) is intentionally untouched, as is the startup CRITICAL warning
in `backend/app/main.py` and the shared audit-key derivation
`derive_audit_key` in `backend/app/services/audit_keys.py`.

## Why

The literal is published in this repository. In single-admin mode it was
both the sole API credential and — via `derive_audit_key`'s admin-token
fallback — the audit HMAC key. All three HMAC'd audit tables derive that
key from `derive_audit_key` (`backend/app/services/audit_keys.py`):
`document_actions` resolves it through `SecretManager.get_hmac_key`
(`backend/app/services/secret_manager.py`; writer call site
`backend/app/api/routes/documents.py:120`), `audit_toggle_log`'s two
writers do the same (`backend/app/api/routes/admin.py:112` and `:172`,
feeding the inserts at `admin.py:81` and
`backend/app/services/maintenance.py:163`), and `security_audit_log`
imports `derive_audit_key` directly
(`backend/app/services/security_audit.py`). Anyone who read the repo
could compute valid digests for all three tables and, holding the
literal as the bearer credential, call the admin routes that
`require_scope`/`require_auth` authenticate. `main.py` only
CRITICAL-warned and continued, and the config layer refused only the
empty token. This closes the symmetric, stronger twin of the JWT-default
refusal already shipped for `USERS_ENABLED=true`, and resolves the
residual ASK recorded in
`docs/releases/pending/987-993-995-audit-auth-boot-followups.md`.

## Migration steps

Single-admin deployments (`USERS_ENABLED=false`) still running
`ADMIN_SECRET_TOKEN=admin-secret-token` will now fail at boot with the
ValueError above. Generate a real token and set it in the environment:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Two operational realities of that boot failure:

- The refusal fires at `Settings` construction during module import,
  before any HTTP surface exists, so the container exits immediately and
  restart policies will crash-loop it until the env / `.env` is fixed.
  There is no in-app rotation path: `admin_secret_token` is not in the
  settings API's `ALLOWED_FIELDS`, so the token cannot be changed through
  the running app.
- The token is also read from the `.env` file (`env_file=".env"`), and in
  pydantic-settings explicit environment variables take precedence over
  `.env` values — if the shell env and `.env` disagree, fix the stale
  source, not just the shell environment.

Audit digests previously produced while the literal was the derived key
should be treated as unverifiable going forward (same rotation note as
the #987-993-995 fragment). Multi-user deployments (`USERS_ENABLED=true`)
are unaffected.

## Known caveats

- Case variants of the literal still boot with no warning at all
  (exact-match decision, consistent with `main.py`'s warning trigger set
  and `backend/tests/test_admin_token_warning.py`'s case-variant pins).
  Case variants do NOT derive the public key — `derive_audit_key()`
  preserves case, so `ADMIN-SECRET-TOKEN` derives `b"ADMIN-SECRET-TOKEN"`,
  a different (operator-specific) key.
- Under `USERS_ENABLED=true` the literal remains a live
  `admin:config` bearer token (the scope map is seeded from it at config
  time); that mode's behavior is intentionally unchanged (AC3).
- Existing audit rows signed under the literal are not rotated or
  re-tombstoned by this change.
- Item B (issue #1001 AC5: the #993 AC2 literal-401 wording decision and
  its cross-post) requires the maintainer's decision comment; the
  contingent literal-401 / RFC 6750 work (AC6) remains out of scope.

## Item B — drafted decision record (AC5, awaiting maintainer posting)

The critic's advisory recommendation (confidence 92/100), consistent with
the frozen check `backend/tests/test_nonascii_credential_rejection.py`
(non-ASCII legs accept any 4xx-never-5xx; wrong-ASCII control legs pin
403/401 per route) and the frontend 401 interceptor
(`frontend/src/lib/api/core.ts` clears `_jwtAccessToken` and calls
`redirectToLogin()` on any unrefreshed 401 — a literal-401 change would
log users out on every wrong admin credential):

> **Decision:** #993 AC2 is confirmed satisfied-as-written: a non-ASCII
> credential receives the route's existing invalid-credential 4xx (403 on
> `require_auth`/`require_scope`/`_resolve_active_user`, 401 on the
> service-account/health paths) and never a 500. No code change; the
> literal-401 / RFC 6750 alignment is declined for this issue and would
> need its own issue/PR (frozen-C2 checkpoint amendment, 403-assertion
> audit across `backend/tests/`, frontend interceptor decision) if ever
> revisited. To be cross-posted to #993 when the maintainer posts this
> decision (AC5).
