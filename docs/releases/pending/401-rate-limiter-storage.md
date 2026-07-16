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
- `_redis_reachable(redis_url)` construction-time probe mirrors
  `CSRFManager.__init__` (`security.py`): if Redis is configured but not
  reachable at startup (e.g. local dev without Redis), the limiter falls back to
  `memory://`. This is required because the autouse test fixture
  `conftest._reset_rate_limiter` calls `limiter.reset()`, and
  `RedisStorage.reset()` issues a real Redis Lua flush that throws
  `ConnectionError` in Redis-less environments — slowapi's `in_memory_fallback`
  only fires inside `_check_request_limit`, not in `reset()`. The probe never
  raises (`except Exception`, like CSRFManager).
- `in_memory_fallback_enabled=True` only when a real Redis backend is wired, so
  a Redis blip *after* startup degrades to a per-process limit instead of
  failing closed (503 on every state-changing route).

### Tests
- `TestStorageUriWiring` (`test_rate_limiting.py`):
  - `_resolve_storage_uri` pure-function cases (redis URL passthrough incl. db
    index/password/TLS; empty/whitespace → `memory://`).
  - `build_limiter` wiring (Redis URL + reachable → `RedisStorage`; empty →
    `MemoryStorage` + no fallback; unreachable → `MemoryStorage`; reachable →
    runtime fallback enabled). These FAIL on pre-fix code (`build_limiter`
    absent) — genuine regression guard.
  - Real (un-mocked) `_redis_reachable` probe tests: returns `False` (never
    raises) for an unreachable host and for malformed URLs. These guard the
    `except Exception` net that prevents a malformed `REDIS_URL` from crashing
    `import app.limiter`.
- Updated one existing source-scan assertion (`test_limiter_instance_exported`)
  from `limiter = WhitelistLimiter` to `limiter = build_limiter` (type guarantee
  still covered by `test_whitelist_limiter_extends_limiter`).

### Docs
- `docs/admin-guide.md` Rate Limiting section: documents that counters are
  stored in Redis under multi-worker deployments, and honestly distinguishes the
  two degraded paths: Redis reachable at startup but blips later → per-worker
  weakening until recovery; Redis unreachable at startup → per-worker mode for
  the worker lifetime (restart to re-share counters); empty `REDIS_URL` (CI) →
  in-memory only.

## Why
#302 finding 3 / #401: the slowapi limiter had no `storage_uri`, so it defaulted
to in-process memory — one bucket set per worker. The admin guide described a
multi-worker "cluster" where Redis is "already used", which could lead an
operator to believe rate limits were safely shared; they were not.

## Migration / configuration
No migration required. Deployments that already set `REDIS_URL` (the documented
Docker setup, which gates the app on `redis: condition: service_healthy`) now
share rate-limit counters across workers automatically. Deployments without
Redis keep the prior per-process behavior (no `REDIS_URL`, or Redis unreachable
at startup).

## Known caveats
- If Redis is unreachable at startup, the limiter runs in per-worker in-memory
  mode for the lifetime of those workers (the limiter is constructed once at
  import; there is no runtime re-probe/recovery state machine). Restart the
  workers once Redis is available to re-share counters. Adding a
  `_check_redis_available`-style re-probe was judged scope creep for a
  complexity-S fix; the documented Docker deployment gates startup on Redis
  health, so this degraded path is not the normal case.
- `in_memory_fallback_enabled=True` (runtime, reachable-Redis path) weakens the
  effective limit by roughly the worker count while Redis is down — an
  availability-over-security trade (the opposite of the CSRF layer's fail-closed
  choice), documented inline and in the admin guide.
