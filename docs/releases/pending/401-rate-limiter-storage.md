# Rate-limiter Redis storage wiring (Issue #401, #302)

## What changed

### Backend — rate-limit counters shared across workers via Redis (#401)
- `backend/app/limiter.py`: the module-global limiter is now constructed via
  `build_limiter(settings.redis_url)` instead of `WhitelistLimiter(key_func=get_client_ip)`.
  Previously no `storage_uri` was passed, so slowapi fell back to per-process
  `MemoryStorage` — under `uvicorn --workers N` each worker kept its own
  counters and the effective rate limit was multiplied by N.
- `build_limiter` passes `storage_uri=settings.redis_url` so all workers hit the
  same Redis counters (the shared backend already used for CSRF tokens and the
  embedding cache), closing the per-worker isolation defect.
- `_resolve_storage_uri(redis_url)` maps an empty/whitespace `REDIS_URL` (the CI
  condition: `.github/workflows/ci.yml` sets `REDIS_URL=""` with no Redis service)
  to `memory://`, since `limits.storage.storage_from_string("")` raises
  `ConfigurationError`.
- `build_limiter` does NOT probe Redis at construction time. The limiter must be
  built at module import (slowapi's `@limiter.limit(...)` decorators in the route
  files run at import and require a concrete `Limiter` instance), so a blocking
  startup probe would delay import by up to ~1s when Redis is unreachable
  (unlike `CSRFManager`/`EmbeddingService`, which can defer to the lifespan
  because they are resolved via FastAPI `Depends`, not decorators). Instead the
  configured `storage_uri` is passed straight to slowapi, which connects lazily
  on the first rate-limited request.
- `in_memory_fallback_enabled=True` whenever a real (non-memory) backend is
  configured, so a Redis failure at request time degrades to a per-process limit
  instead of failing closed (503 on every state-changing route). slowapi
  re-probes the backend with exponential backoff and returns to Redis when it
  recovers.

### Backend — Redis URLs are redacted in logs (F-001)
- `backend/app/utils/secrets.py` (new): `redact_url(url)` masks any embedded
  `user:password@` in a connection string (Redis/DB URIs) to `user:***@`,
  preserving scheme/host/port/db. Handles `redis://`, `rediss://`,
  `redis+sentinel://`, no-credential URLs (passthrough), and unparseable strings
  (never raises — safe in a logging path).
- `backend/app/limiter.py`: the "wired to Redis" log uses `redact_url` so a
  `REDIS_URL` carrying a password is not leaked to log aggregators.
- `backend/app/services/embeddings.py`: the "Embedding Redis L2 cache connected"
  log (previously the most severe leak — unconditional on every boot) now uses
  `redact_url`.

### Tests
- `TestStorageUriWiring` (`test_rate_limiting.py`):
  - `_resolve_storage_uri` pure-function cases (redis URL passthrough incl. db
    index/password/TLS; empty/whitespace → `memory://`).
  - `build_limiter` wiring: Redis URL → `RedisStorage` (+ asserts the resolved
    URI is passed through and that the result is a `WhitelistLimiter` — the
    type-anchor that catches a regression dropping the whitelist bypass);
    empty → `MemoryStorage` + no fallback; Redis URL → runtime fallback enabled.
    These FAIL on pre-fix code (`build_limiter` absent) — genuine regression guard.
  - `test_module_global_limiter_is_whitelist_limiter`: runtime `isinstance` guard
    on the imported module global (PRR-005).
- `TestRedisUrlRedaction` (`test_rate_limiting.py`): `redact_url` masks password
  / user+password, passes through no-credential URLs, handles TLS + sentinel (F-001).
- `backend/tests/conftest.py`: the autouse `_reset_rate_limiter` fixture now
  swallows any exception from `limiter.reset()` (not just ImportError/AttributeError),
  so running the suite with a non-empty `REDIS_URL` but no reachable Redis does
  not crash every test via `RedisStorage.reset()`.
- Updated one existing source-scan assertion (`test_limiter_instance_exported`)
  from `limiter = WhitelistLimiter` to `limiter = build_limiter`.

### Docs
- `docs/admin-guide.md` Rate Limiting section: documents that counters are
  stored in Redis under multi-worker deployments and the runtime fallback
  behavior (per-worker weakening until Redis recovers).
- `docs/release.md` infrastructure checklist: Redis line now notes it backs CSRF,
  the embedding cache, AND shared rate-limit counters.

## Why
#302 finding 3 / #401: the slowapi limiter had no `storage_uri`, so it defaulted
to in-process memory — one bucket set per worker. The admin guide described a
multi-worker "cluster" where Redis is "already used", which could lead an
operator to believe rate limits were safely shared; they were not. The redaction
hardening (F-001) closes a credential-leak path in the limiter and (more
severely) the embedding service's unconditional connection log.

## Migration / configuration
No migration required. Deployments that already set `REDIS_URL` (the documented
Docker setup, which gates the app on `redis: condition: service_healthy`) now
share rate-limit counters across workers automatically. Deployments without
Redis (empty `REDIS_URL`, e.g. CI) keep the prior per-process in-memory behavior.

## Known caveats
- The limiter is constructed once at import with the configured `storage_uri`;
  it does not probe Redis at construction (to avoid blocking import) and there
  is no startup-time fallback to `memory://`. If Redis is down at startup, the
  first request triggers slowapi's lazy connect + `in_memory_fallback` flip
  (per-process limit until Redis recovers via exponential-backoff re-probe).
  The documented Docker deployment gates startup on Redis health, so this is an
  edge case, not the normal path.
- `in_memory_fallback_enabled=True` (when Redis is configured) weakens the
  effective limit by roughly the worker count while Redis is down — an
  availability-over-security trade (different from the CSRF layer's fail-closed
  choice), documented inline and in the admin guide.
