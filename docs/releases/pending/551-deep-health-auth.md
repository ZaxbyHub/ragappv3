# 551 — Gate and rate-limit unauthenticated provider probes on deep health (issue #551)

## What changed

- `backend/app/api/routes/health.py`:
  - `GET /api/health` now requires authentication **whenever the request
    asks for the deep probe** — any spelling FastAPI/pydantic parses as true
    (`deep=true`/`1`/`yes`/`on`/`t`/`y`, any case) — via the new
    `require_deep_health_probe_auth` dependency (a deep-only wrapper: the
    shallow poll that the frontend banner and compose-adjacent tooling rely
    on is unchanged and stays anonymous-open).
  - `GET /api/llm-health/modes` now requires authentication via
    `require_health_probe_auth` (the frontend chat composer calls it
    post-login; on 401 its store already fails closed and disables the mode
    toggle).
  - Both routes carry `@limiter.limit(settings.health_probe_rate_limit)`.
    On `/api/health` the limit is deep-only (slowapi `exempt_when` skips the
    bucket for shallow polls), so the anonymous heartbeat contract is
    unchanged even behind NAT-shared egress IPs. `/llm-health/modes` is
    limited on every call. A caller presenting
    `X-API-Key: $HEALTH_CHECK_API_KEY` is exempt from the limits via the
    pre-existing limiter whitelist.
- `backend/app/api/deps.py`: new `require_health_probe_auth` /
  `require_deep_health_probe_auth` dependencies. They accept either an
  authenticated user (the existing `get_current_active_user` mechanism,
  honoring `app.dependency_overrides` exactly like
  `get_current_user_or_service_account`) or the
  `X-API-Key: settings.health_check_api_key` monitoring credential
  (constant-time compare; fail-closed when the setting is empty).
- `backend/app/limiter.py`: `WhitelistLimiter._check_request_limit` now sets
  `request.state.view_rate_limit = None` on the whitelist early-return.
  slowapi's limit decorator reads that attribute unconditionally after the
  handler returns (even with headers disabled), so on master **every
  whitelisted request to any `@limiter.limit`-decorated route returned 500**
  (pre-existing latent defect, empirically reproduced with a minimal app
  using the repo limiter; surfaced by the new health limits).
- `backend/app/config.py` + `.env.example` + `docker-compose.yml` +
  `scripts/check_config_contract.py`: new `HEALTH_PROBE_RATE_LIMIT`
  (default `30/minute`, same scale as chat) contract-enforced across all
  three surfaces.
- `frontend/src/hooks/useHealthCheck.ts`: sends `deep=true` only when the
  user is authenticated (the hook is mounted at the App root, login page
  included; an unauthenticated deep probe would 401 and flap the reconnect
  banner). Shallow polls are unchanged and still get truthful last-known
  service status from the server-side cache.

## Why (issue #551, finding C12)

`deep=true` runs the full provider sweep inline per request (real
`chat_completion(max_tokens=1)` pings per configured LLM client, an embedding
probe, and model listing), and `/llm-health/modes` pings both LLM clients —
both with no authentication and no rate limit, so any network caller could
drive unbounded outbound provider generations (audit-measured 10.7 s / 6.4 s
per call with unreachable providers).

## Breaking change / operator action required

- **External monitoring** hitting `deep=true` or `/llm-health/modes`
  anonymously will now get 401. Either set `HEALTH_CHECK_API_KEY` and send
  `X-API-Key: <key>` (this credential also bypasses the new rate limit), or
  authenticate as a user. **Deployment prerequisite:** with the default
  empty `HEALTH_CHECK_API_KEY`, the X-API-Key path is dead by design — set
  the key before upgrading if monitoring depends on it.
- Unauthenticated **shallow** `GET /api/health` and `GET /api/healthz` are
  unchanged (frontend heartbeat, compose healthcheck): not auth-gated and
  not rate-limited — the `HEALTH_PROBE_RATE_LIMIT` bucket is consumed only
  by deep probes and the mode probe.
- **External monitoring of only the shallow status** keeps working with no
  credentials; monitoring `deep=true` or `/llm-health/modes` needs the key
  or a session. With docker-compose's empty default
  `HEALTH_CHECK_API_KEY=`, key-based deep monitoring is disabled until the
  operator sets the key (`.env.example` ships a placeholder value).
- `GET /api/health/vector-reconciliation` already required a user; unchanged.

## Rollback

Revert the change: removes the auth dependencies, the two `@limiter.limit`
decorators, the `HEALTH_PROBE_RATE_LIMIT` setting, and the frontend auth
gate; the `limiter.py` whitelist fix is independent and safe to keep.

## Tests

- `backend/tests/test_health_deep_auth.py` (new): unauthenticated deep/modes
  → 401 with zero provider probes; authenticated deep keeps the full
  payload; X-API-Key authenticates both routes (wrong key → 401 on both);
  the limiter blocks authenticated call N+1 within the window (429) while
  the key holder bypasses it; shallow `/health` and `/healthz` stay open.
- `frontend/src/hooks/useHealthCheck.issue551.test.tsx` (new): the hook
  sends `deep=true` only when authenticated.
- Existing suites updated for the gate: `test_api.py`, `test_api_routes.py`,
  `test_issue494_ac29_health_cache_singleflight.py`, `test_integration.py`
  (drive deep health as authenticated callers; limiter storage reset for
  suite isolation), `useHealthCheck.issue494.test.tsx` (mount deep check now
  runs as an authenticated user).
- `backend/tests/test_health_route_guardrail.py` (new): source-level
  guardrail — every route in `health.py` that carries a provider-checker
  dependency must declare a health-probe auth dependency and a
  `@limiter.limit` decorator, so the gate cannot silently regress.
