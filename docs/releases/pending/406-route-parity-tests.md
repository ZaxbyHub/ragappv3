# Route parity + settings hardening + vault test coverage (Issues #406, #389, #390)

## What changed

### Backend — route parity (issue #389)
- **F-PRE-001 — organizations/vault_members non-slash variants**:
  `organizations.py` and `vault_members.py` registered only the trailing-slash
  list routes (`@router.get("/")` under a prefix), so non-slash requests
  307-redirected (FastAPI `redirect_slashes=True` default). Added the
  `@router.{get,post}("", include_in_schema=False)` duplicate for the list GET
  and POST on both `router` and `vault_members.py`'s `group_access_router`,
  matching the users.py / groups.py convention.
- **F-PRE-002 — documents.py duplicate OpenAPI entries**: the GET and POST list
  endpoints had both slash and non-slash decorators but neither was marked
  `include_in_schema=False`, producing two OpenAPI paths. Added
  `include_in_schema=False` to the non-slash variant (dedupes the schema).
- **F-PRE-004 — settings POST/PUT hardening**: POST and PUT `/settings` lacked
  `response_model` and `@limiter.limit`. Added
  `response_model=SettingsResponse` (wire-safe — handlers already return
  `SettingsResponse.model_validate(...)`) and
  `@limiter.limit(settings.admin_rate_limit)` (matches the admin-mutation idiom
  in documents.py / memories.py). GET `/settings` is unchanged.

  **Shared-bucket behavior (intentional):** POST and PUT `/settings` share a
  single `admin_rate_limit` (10/minute) bucket — slowapi's default
  `key_style="url"` does not split by HTTP method. This is the desired
  behavior: an admin hammering settings mutations should be collectively
  throttled regardless of verb, not given 20/minute by alternating POST/PUT.
  Verified by `test_post_and_put_share_one_bucket`.

### Backend — rate-limit quota normalization (PRR-001, pre-existing defect closed here)
- **`WhitelistLimiter._check_request_limit`** now normalizes the request scope
  path (`rstrip("/")`) before delegating to slowapi. slowapi's default
  `key_style="url"` uses `request["path"]` verbatim as the bucket key, so any
  route registered with both slash and non-slash variants (settings, vaults,
  memories, organizations, vault_members, group-access) previously had TWO
  independent buckets per method — a client could double the effective limit
  by alternating paths. The normalization mirrors what `main.py:130` and
  `maintenance.py:31` already do. The scope swap is scoped to the limit check
  (restored in `finally`) so handlers still see their original path; routing
  has already completed before this runs. Regression guard:
  `test_alternating_slash_and_non_slash_shares_bucket`.

### Backend — limiter headers guard (PRR-014, latent defect)
- **`build_limiter`** now forces `limiter_instance._headers_enabled = False`
  after construction. The rate-limited handlers in this codebase do NOT
  declare a `response: Response` parameter, so slowapi's `_inject_headers`
  post-response path would raise if headers were ever enabled. Passing
  `headers_enabled=False` to the constructor is NOT sufficient on its own —
  slowapi's `Limiter.__init__` ORs the constructor value with the
  `RATELIMIT_HEADERS_ENABLED` env var (`False or True == True`), so an
  operator could re-enable headers via env and resurrect the exception. The
  post-construction force-assignment overrides the OR result and is the
  effective guard; enabling headers later now requires a code change to this
  line AND an audit of every rate-limited handler (chat, search, vaults,
  memories, documents, settings, auth) to add the `Response` param first.
  Regression guard: `test_build_limiter_forces_headers_disabled_even_with_env_var`.

### Tests — vault coverage (issue #390)
- **F-004**: added `test_vault_response_org_id_member_role` — exercises the
  org_id field through the member role, not just superadmin.
- **F-005**: extended `test_list_vaults_includes_org_id_field` with a positive
  `org_id == <value>` assertion for an org-scoped vault (previously only the
  global `org_id=None` case was asserted).
- **F-006**: extended `test_accessible_vaults_org_member_sees_org_vault` to
  assert the `org_id` field is present and correct in the accessible-endpoint
  response.
- **F-007**: added `test_org_vault_org_id_preserved_after_name_change` — PUTs
  `name` only (the existing test only PUTs `description`) and asserts org_id
  is preserved.
- **F-PRE-003**: `test_vaults.py`'s module-level `tempfile.mkdtemp()` (from
  `setup_test_db()`) is now cleaned up via `atexit.register(shutil.rmtree, ...)`.
  Per-test temp dirs were already cleaned in `tearDown`.

### Tests — permanent regression proofs (new)
- `backend/tests/test_route_parity.py` (new, 12 tests): OpenAPI single-entry
  assertions + runtime parity assertions (with `follow_redirects=False`) for
  organizations, vault_members, and group-access; SettingsResponse schema
  assertions for POST/PUT `/settings`.
- `backend/tests/test_rate_limiting.py::TestRateLimitingDecoratorsSettings`
  (new class, 4 tests): source-inspection asserting the settings limiter import
  and decorators, mirroring the existing `TestRateLimitingDecoratorsVaults`
  idiom.

### Tooling — SAST baseline
- `backend/security/bandit-baseline.json` regenerated: the pre-existing B608
  finding at `organizations.py:374` (a false-positive on a parameterized UPDATE
  whose `updates` list is built from hardcoded literals) shifted to `:376`
  because this PR added two decorator lines earlier in the file. Pure
  line-number shift — no new vulnerability. (Per `scripts/run_bandit.py`
  docstring, this is the designed workflow.)

## Why
Follow-up from PR #157 review (#200). The route-parity and test-coverage items
were filed as #389 and #390 and bundled into tracking issue #406.

## Migration steps
No migration required. The only wire-visible change is that POST and PUT
`/settings` now declare a response schema in the OpenAPI doc (the response body
is unchanged). Both slash and non-slash variants of all touched list routes
continue to resolve.

## Known caveats
- The four new `test_vaults.py` tests (F-004/005/006/007) are **coverage tests**,
  not regression proofs: they assert already-correct behavior and pass on
  pre-fix code. The real regression proofs for this PR's source changes live in
  `test_route_parity.py` and `TestRateLimitingDecoratorsSettings`.
- The `group_access_router` parity additions (`vault_members.py:303,358`) are
  convention-driven (same file, same defect as the named `vault_members` routes)
  but beyond the literal scope of #389, which names only organizations.py and
  vault_members.py. Flagged for reviewer discretion.
- An **inverse** parity gap exists in other route files not touched by this PR:
  `folders.py:92,104`, `prompts.py:70`, `service_accounts.py:91,152`,
  `tags.py:84,96` register only the `""` (non-slash) variant. Their slash
  variants 307-redirect and their visible schema entry uses the opposite
  convention from the five prefix-router files normalized here. Out of scope for
  #389/#406; worth a separate follow-up issue.
