# fix(api,middleware): auth cache race, loud schema fallback, users pool holds, bounded health probes, live mode health, host exemption, 500 access lines (#798, #1179)

## What changed

- **Active-user cache write-back race (#798 AC2):** invalidations now bump a
  generation counter; a read whose generation changed mid-fetch never
  re-caches its snapshot (`deps.py`, maintenance/toggle_manager pattern).
- **Loud schema failure (#798 AC3):** `_fetch_user_row_with_pwc` no longer
  silently substitutes `password_changed_at = 0.0` when the column is
  missing — the `OperationalError` propagates. Test fixtures that hand-roll
  a `users` table must carry the column (the production-schema helper
  already does).
- **Users routes hold one pooled connection (#798 AC4):** the ten
  `users.py` handlers that opened a second pool connection per request now
  use the request-scoped `Depends(get_db)` connection (the #205 pattern;
  same connection the auth dependency already holds).
- **Bounded health probes (#1179 AC1):** `check_chat` and `_probe_client`
  send `max_tokens=1` and wrap the call in `asyncio.wait_for` at the
  checker's short timeout — an injected production client's own 300 s
  transport timeout can no longer hang a health endpoint.
- **Mode health reflects live clients (#1179 AC2):** late activation of the
  thinking or instant client wires it into `LLMHealthChecker` (new
  `rebind_clients`), and `check_chat_modes` gates each mode probe on its
  CURRENT settings endpoint pair — a cleared (parked) pair reports the
  mode unavailable without probing the parked client.
- **Host exemption actually works (#1179 AC3):** the health-path exemption
  on `TrustedHostMiddleware` was dead code (a `dispatch()` override the
  pure-ASGI base never calls); it is now a `__call__` override, and
  `/api/healthz` joined `/health` and `/api/health` in the exempt set.
  Every non-health path keeps full host validation.
- **500s get an access line (#1179 AC4):** the access-logging middleware's
  new exception arm emits exactly one `http_request` record
  (`status_code: 500`) before re-raising. `asyncio.CancelledError`
  (BaseException) is not recorded as a 500. The admin guide and the
  middleware-ordering comment were updated; the no-`X-Request-ID`-on-500s
  caveat in `docs/release.md` still holds.

## Why

Collated audit bundle #798 (auth dependency and connection-pool
correctness) + #1179 (health probes reflect live clients and host policy);
all seven in-scope members verified RED at the base and GREEN after this
change, plus absorbed deployment-audit evidence on the shallow-/health
per-call queue-size probe (investigated as an unverified mechanism
candidate; the pool-hold fix removes one saturation source).

## Migration

No schema or config migration. Databases whose startup migration was
skipped or failed now fail auth LOUDLY instead of silently disabling
password-change token invalidation — that is the intended fail-fast.
Be aware of the concrete shape: every authenticated password-user request
returns a generic 500 (admins included) and `/api/healthz` reports 503
until the operator repairs the schema. Run migrations (normal startup
does) to restore service. If startup migration failed on a deterministic
step, a restart does NOT converge — the failing step refuses every boot,
so repair the schema out-of-band, then restart.

## Rollback

Revert the PR; the silent fallback returns on revert only for such
degraded schemas. Note that bounded health-probe timeouts no longer
charge the LLM circuit breaker (the probe's inner call is cancelled, so
`record_failure` is skipped) — user traffic still charges it normally.

## Caveats

- Direct Python callers of the ten migrated `users.py` handlers must now
  pass `db=` (FastAPI requests are unaffected).
- The health checker is wired only through settings late-activation;
  checker-less test applications are a no-op (getattr guard).
