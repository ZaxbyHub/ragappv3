# JWT_ALGORITHM is honored (HS-family allowlist); CSRF test policy declarations; #202 test-gap closures (Issue #202)

## What changed

### JWT_ALGORITHM wired and validated (ENH-012)
- `backend/app/services/auth_service.py:get_jwt_config()` now reads
  `settings.jwt_algorithm` instead of a hardcoded `HS256` constant, and validates
  it against the symmetric HS family — `HS256`, `HS384`, `HS512`. Any other value
  raises `RuntimeError` naming the offending value and the supported set. Both
  token minting (`create_access_token`) and verification (`decode_access_token`)
  consume this one function, so the configured algorithm is enforced identically
  on both sides.
- `verify_auth_config()` gains the same membership check so it cannot drift from
  `get_jwt_config` if it is ever wired to startup (it remains unwired today — see
  Known limitations).
- `backend/app/config.py` (field docstring) and `.env.example` document the
  allowlist and the rotation semantics.

### Explicit CSRF test-policy declarations (ENH-008)
- `backend/tests/conftest.py:_module_manages_csrf` now honors an explicit
  module-level `CSRF_TEST_POLICY = "naive"` (CSRF bypass allowed for that test
  module) or `CSRF_TEST_POLICY = "manages"` (module exercises real CSRF
  enforcement) declaration ahead of the legacy source-substring scan. An
  unrecognized value or no declaration keeps the existing substring default, so
  every existing test module classifies exactly as before. The contract is pinned
  by `backend/tests/test_csrf_classifier_policy.py`.

### Test-gap closures from the #202 replan
- Successful-login reset of `failed_attempts`/`locked_until` pinned
  (`test_login_resets_failed_attempts.py`).
- `vault_members` / `vault_group_access` cascade on vault delete pinned
  (`test_vault_delete_cascade_membership.py`).
- End-to-end org membership → group membership → `vault_group_access` → effective
  permission chain pinned on the real schema, with a no-direct-membership
  precondition and a negative control (`test_org_group_vault_chain.py`).
- `must_change_password` exempt-path classification guard
  (`test_exempt_paths_guard.py`): every mounted `/auth` route must be consciously
  classified (exempt / me-read / explicitly blocked) or the guard fails.
- Frontend CSRF resilience pins (`frontend/src/lib/api/api core.csrfgaps.test.ts`):
  `purgeStaleCsrfCookies` app-root exclusion, `ensureCsrfToken(true)` cache/cookie
  bypass, and 403-with-blob-body decode feeding the CSRF retry.
- `deps.py` `UserRole` documents the two-role model (system-wide `users.role` vs
  per-org `org_members.role`).
- Already-fixed #202 surfaces (auth transaction atomicity, refresh-family
  revocation, change-password strength branches, groups CRUD coverage, compose
  digest pins, prompt-escape suites) are pinned green by the issue-trace
  acceptance checks.

## Operator notes

- **`JWT_ALGORITHM` is now enforced.** Previously the setting was silently
  ignored (HS256 was always used). Deployments that set a value outside
  `HS256`/`HS384`/`HS512` — for example `RS256`, `none`, `HS1`, a lowercase
  `hs256`, or a value with stray whitespace — will now get a `RuntimeError` at
  the first token operation (typically the first login) after upgrading. The
  error names the offending value and the supported set. Set
  `JWT_ALGORITHM=HS256` (exact case, no whitespace) to restore the previous
  effective behavior.
- **Rotating `JWT_ALGORITHM` invalidates outstanding tokens** minted under the
  previous algorithm (sessions must re-authenticate). This is pinned by test.
- `test_login_resets_failed_attempts.py` installs a test CSRF manager on the
  shared app singleton's `app.state` and does not restore it on teardown —
  a known test-hygiene limitation shared with the sibling suite pattern.
- `verify_auth_config()` still has no startup caller (pre-existing); an invalid
  `JWT_ALGORITHM` therefore surfaces at the first token operation, not at boot.
  Wiring it into startup was deliberately not done here: `app/lifespan.py`
  follows a fail-soft startup doctrine.

## Rollback

Revert the PR, or set `JWT_ALGORITHM=HS256` in the environment — the algorithm
is read fresh on every token operation, so no restart-specific migration state
exists. The CSRF declaration mechanism is test-only and cannot affect production
enforcement (`security.py` requires `PYTEST_CURRENT_TEST` for the bypass).

## Known limitations

- `verify_auth_config()` remains unwired at startup (pre-existing; recorded as a
  deliberate follow-up, not a regression of this PR).
- The proposed lint rule against raw f-string LLM prompts (ENH-013 bundle) is
  superseded by the existing mutation-tested escape suites
  (`test_prompt_builder_xml_escape.py`, `test_prompt_injection_defense.py`).
