# 724: CSRF enforcement on admin/service-account mutations, CSRF store failover and at-rest hashing, rotation grace

Issue: #724 ([Workstream D] PR 6 of 8 — audit findings T1-29-K-12, S06-SK-06, TQ-wiring-all-03, T1-21-S-02, RT-S2-03, T1-21-S2-05, T1-21-K-11, S02-SK2-11, T1-01-K-06, T1-01-S-09).

## What changed

- **`POST /api/admin/toggles` and the three service-account mutations
  (create/rotate/revoke) now enforce `csrf_protect`**, matching the sibling
  admin:config routes' documented convention (previously enforced on the
  maintenance sibling and prompts.py only).
- **`/admin/toggles` handles the pool's `RuntimeError`** (checkout
  exhaustion/closure) with a 503 instead of an unhandled 500, while the
  sqlite write-failure 500 mapping and the audit-HMAC rollback atomicity
  are unchanged. The write-only `app.state.model_validation` flag is
  deleted (lifespan + the post-commit refresh): nothing ever read it.
- **The CSRF manager fails over after a healthy boot.** Redis dying
  mid-run now engages the durable SQLite fallback (lazily constructed,
  lock-published) and retries the token operation instead of 503-ing every
  request until Redis returns; a failed retry keeps the 503 mapping. The
  recovery ping is rate-limited (30s window) and the Redis client gets a
  2s connect timeout, so a dead Redis no longer adds ~2s to every token
  operation (previously measured at 8.2s on `/api/auth/register`).
- **The SQLite fallback store gets a 30s lock timeout** (matching the app
  pool's busy_timeout) and **stores SHA-256 hashes, not raw tokens**, in
  `csrf_tokens.token_hash` — the column finally matches its name. Pre-fix
  rows are upgraded transparently on first lookup (expiry preserved, raw
  row deleted in the same step); delete carries the same legacy arm.
- **CSRF-issuance failures during register/login/refresh are logged**
  (WARNING with `exc_info`) instead of silently swallowed — a client
  getting 200 with no CSRF cookie is now visible in the logs.
- **Service-account key rotation has a bounded grace.** The previous key
  stays valid until the new key is first used or 24h elapse
  (`previous_key_hash`/`previous_key_expires_at`, additive migration), so
  an undelivered rotation response no longer strands an integration with a
  dead old key and an unusable new one. Both live validators
  (`require_service_account` and the document/search/tag dependency)
  resolve through one shared `lookup_service_account` helper. Revocation
  still kills both keys immediately.

## Known residual boundaries (disclosed)

- A pool checkout-exhaustion `RuntimeError` raised during dependency
  resolution (`get_db`) precedes any route handler and remains unhandled
  globally — out of this issue's scope (its AC names only the toggle write
  and the post-commit refresh sites).
- Tokens issued on one store miss on the other after a failover/recovery
  flip (browser clients self-heal via the CSRF interceptor retry; raw API
  clients must re-fetch a token).
- `TogglePayload.feature` still accepts and persists arbitrary strings
  (pre-existing behavior; no reader constrains the set).

## Verification

- `backend/tests/test_d06_csrf_store_and_admin.py` (12 frozen checks,
  issue-tracer red checkpoint at base 4323afbb) and
  `backend/tests/test_d06_guardrails.py` (7 guardrails: the mutation-route
  CSRF census over both routers, grace-TTL expiry, legacy-row upgrade,
  failover round-trip, deps Path-2 grace leg).
- The two prior immediate-invalidation rotation tests are rewritten to the
  grace semantics per the issue's AC10 note.
