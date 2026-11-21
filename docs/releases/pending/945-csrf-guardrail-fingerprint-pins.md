# CSRF guardrail follow-up: secondary-router scan verified; dedicated pins for mutating-route CSRF and admin scope fingerprint

Issue: #945 (follow-up to PR #890 / #709)

## What changed

- **Verified, no change needed — CSRF guardrail secondary-router scan (AC1).**
  `backend/tests/test_c04_csrf_scope_guardrail.py` already enumerates every
  module-level `APIRouter` (via `vars(module)`, not just the conventional
  `router` attribute), so secondary routers such as
  `backend/app/api/routes/vault_members.py`'s `group_access_router` are inside
  the scan; `test_guardrail_discovery_covers_secondary_routers` pins that.
  This change re-verified the claim with a runtime mutant: a GET route with
  `csrf_protect` injected onto `group_access_router` is reported as an
  offender by the repo-wide scan.
- **Mutating-prompts CSRF retention pin moved into the dedicated file (AC2).**
  `test_mutating_prompts_routes_still_depend_on_csrf_protect` moved from
  `backend/tests/test_c04_review_hardening.py` to
  `backend/tests/test_c04_prompts_read_csrf.py` — the file issue #945's
  acceptance check names — so one module pins both directions: prompts GET
  routes must NOT depend on `csrf_protect`, and the four mutating routes
  (POST `/prompts`, POST `/{version}/activate`, POST `/ab-experiments`,
  POST `/ab-experiments/{id}/end`) must KEEP it. Verified discriminating:
  removing the dependency from POST `/prompts` fails the suite.
- **New dedicated `admin_scope_fingerprint` pin (AC3).**
  `backend/tests/test_admin_scope_fingerprint.py` pins
  `admin_scope_fingerprint` from `backend/app/security/__init__.py` on all
  five axes from the issue: 16 lowercase-hex characters on the function's
  actual output (charset, not just length); the fixed-scope expected digest
  pinned as a literal; order independence fed deliberately unsorted scope
  lists (a `sorted()`-dropping mutant fails); the empty-scope sentinel
  (`sha256("")[:16]` for an empty list and an unmapped token); and equality
  with the service-account `created_by` formula — a live
  `POST /api/service-accounts` (route in
  `backend/app/api/routes/service_accounts.py`) must persist exactly
  `admin_scope_fingerprint(<creating admin token>)`, the same helper the
  audit-redaction migration in `backend/app/models/database.py` matches.
  These close the three residual gaps the #945 review found in
  `test_admin_scope_fingerprint_shape` (non-discriminating order check, no
  hex-charset check, no created_by equality assertion).
- **AC4 (preserving)**: GET prompts routes' scope/auth dependencies are
  untouched; `TestListExperimentsEndpoint` in
  `backend/tests/test_prompt_ab_variants.py` passes unchanged.

## Why

The #709/#890 swarm review (reviewer C7, CONFIRMED/LOW) found the CSRF
guardrail overstated its coverage ("any GET route in any API router") while
only walking the conventional `router` attribute, and that the sibling pins
around the fix — mutating routes keep the handshake, the audit fingerprint
helper keeps its shape — were missing or non-discriminating. Test-only
change; no production code was modified.

## Migration steps

None. Test-only; no runtime, schema, config, or API changes.

## Known caveats

- The repo-wide CSRF scan still cannot see dependencies attached at
  app-assembly time via `app.include_router(..., dependencies=[...])` in
  `backend/app/main.py`; none exist today (census in the guardrail
  docstring) and none may be added for GET routes.
- `test_admin_scope_fingerprint_shape` remains in
  `backend/tests/test_c04_review_hardening.py` (weaker on order
  independence, but harmless); the complete pin lives in the new dedicated
  file.
