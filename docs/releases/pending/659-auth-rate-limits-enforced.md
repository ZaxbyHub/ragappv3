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
client IP). This is the intended behavior change. Health-check probes carrying
a valid `X-API-Key` remain exempt via the existing whitelist. Deployments behind
a trusted reverse proxy that need per-client attribution must keep
`trust_proxy_headers` enabled so the key uses X-Forwarded-For; otherwise the
direct connection IP is the bucket key.

## Known caveats

- Deferred follow-ups are unchanged: `logout`, `setup-status`, and `me`
  remain intentionally unthrottled (asserted by the guard suite).
- The register limit (5/hour) predates the first-setup wizard (#622/#644);
  if multi-operator onboarding ever needs a higher ceiling, the policy values
  (source + guard test + runtime tests) must move together.
