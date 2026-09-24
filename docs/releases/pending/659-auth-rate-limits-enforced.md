# Auth rate limits now enforced on register/login/refresh (issue #659)

## What changed

### Backend (issue #659 — F1/F2/F3)

- **Decorator order fixed on three auth routes**: `POST /api/auth/register`,
  `POST /api/auth/login`, and `POST /api/auth/refresh` had `@limiter.limit(...)`
  stacked ABOVE `@router.post(...)`. Python applies decorators bottom-up, so
  the router registered and dispatched the UNWRAPPED function and the limiter
  wrapper was discarded at import time — the limits never executed and no
  request was ever throttled. The three sites now use the repo's load-bearing
  order (`@router.post` outermost, `@limiter.limit` directly below), matching
  `change-password` (PR #418, MED-8) and the other 35 correct limiter sites.
  No signature or policy change: the documented values are now actually
  enforced — register 5/hour, login 10/minute, refresh 30/minute, keyed per
  client IP (`get_client_ip`; X-Forwarded-For first-hop only when
  `trust_proxy_headers` is enabled).
- **Guard test inverted**: the three source-scan regexes in
  `backend/tests/test_auth_rate_limiting.py` required the broken order
  (limiter above router) and passed because the routes were broken; they now
  require the correct order, and the change-password docstring's
  "follow-up should correct the others" deferral is resolved.
- **New real-ASGI runtime tests** (`backend/tests/test_auth_route_rate_limit_runtime.py`):
  drive the actual routes through TestClient and assert the 11th login,
  6th register, and 31st refresh inside the window return HTTP 429.
- **New repo-wide AST guard** (`backend/tests/test_issue659_no_limiter_above_router.py`):
  fails the build if any function under `backend/app/` ever stacks
  `@limiter.limit` above a route decorator again (with synthetic-fixture
  positive controls).

## Why

The three routes were the only inverted limiter sites in the codebase (38
`@limiter.limit` sites total; 3 broken, 35 correct). Register/login/refresh
were unthrottled brute-force surfaces despite the decorators being visibly
present — the defect class fails silently, and the guard test authored against
the code as-found actively pinned the broken pattern.

## Migration / deployment impact

**Operators: the documented limits are live for the first time.** Previously,
scripted or brute-force traffic against `/api/auth/register`, `/api/auth/login`,
and `/api/auth/refresh` passed unthrottled; it may now receive HTTP 429 once
the window is exceeded (5/hour register, 10/minute login, 30/minute refresh per
client IP). This is the intended behavior change.

**Health-check `X-API-Key` exemption scope.** Requests carrying a valid
`X-API-Key` (matching `settings.health_check_api_key`) bypass rate limits via
the existing whitelist in `backend/app/limiter.py:_should_whitelist`. The
exemption is route-agnostic by design and applies to **every
`@limiter.limit`-decorated route** — including the newly enforced
`register`/`login`/`refresh`. **Operators should treat
`HEALTH_CHECK_API_KEY` as a high-value credential, not just a monitoring
token**; the same value also authenticates `GET /api/health?deep=true` and
`GET /api/llm-health/modes` via
`backend/app/api/deps.py:require_health_probe_auth`. See `docs/engineering`
for the recommended provisioning model.

**Bucket-key caveats.** Deployments behind a trusted reverse proxy that
need per-client attribution must keep `trust_proxy_headers` enabled so the
key uses `X-Forwarded-For`; otherwise the direct connection IP is the bucket
key. With `trust_proxy_headers=True` and a proxy that does not strip
client-supplied `X-Forwarded-For`, an attacker who can speak HTTP directly
to the app bypassing the proxy can rotate the header to mint fresh
per-IP buckets. The 5/10/30-minute limits apply per CSRF-validating
request; requests that fail CSRF or body validation return 403/422 BEFORE
the limiter wrapper runs (slowapi's wrapper executes after
`solve_dependencies`), so the bucket is not consumed for those.

**Non-ASCII `X-API-Key`.** A client sending a non-ASCII `X-API-Key` (e.g.
raw byte `0xE9`, latin-1 decoded) does NOT bypass limits and does NOT 500:
`_should_whitelist` returns False on non-ASCII and the request falls
through to the bucket check.

## Known caveats

- Deferred follow-ups are unchanged: `logout`, `setup-status`, and `me`
  remain intentionally unthrottled (asserted by the guard suite).
- The register limit (5/hour) predates the first-setup wizard (#622/#644);
  if multi-operator onboarding ever needs a higher ceiling, the policy values
  (source + guard test + runtime tests) must move together.
