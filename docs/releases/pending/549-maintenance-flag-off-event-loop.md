# 549 — Workstream G2: maintenance flag off the event loop, sign-in reachable during maintenance

## What changed

- `MaintenanceMiddleware.dispatch` no longer performs a synchronous pooled
  SQLite read on the event loop for every request. GET/HEAD/OPTIONS requests
  never consult the maintenance flag (DB-free liveness routes stay fast and
  pool-free under pool exhaustion); mutating methods read the flag through a
  short in-process TTL cache (5 s) filled off the loop via `asyncio.to_thread`.
- Sign-in routes are exempt from the maintenance block: `POST /api/auth/login`,
  `/api/auth/refresh` and `/api/auth/logout` stay reachable while the flag is
  set, so a maintenance window longer than the access-token lifetime no longer
  logs every user out with no way back in. `POST /api/auth/register` stays
  blocked (a window is a data freeze for account creation).
- Any failure of the flag read (pool exhaustion, corrupted `system_flags` row)
  now fails OPEN with a WARNING from `app.middleware.maintenance` instead of
  surfacing as an uncaught RuntimeError/500 on every route.
- `BackgroundProcessor.enqueue`'s maintenance check moved off the event loop
  and onto the same cached, fail-open read (same defect class as the
  middleware finding).
- `MaintenanceService.set_flag` invalidates the cache on commit: same-process
  toggles are visible to the very next request; cross-process toggles within
  the 5 s TTL. `get_flag()` remains the raw uncached read used by the admin
  toggle route's response.

## Migration

- No schema, config-key, or API-shape changes. Behavior only.

## Breaking changes

- None in payload shapes. Behavioral notes: auth login/refresh/logout no
  longer return 503 during maintenance; non-mutating requests no longer
  observe a maintenance-flag read at all; a flag-read failure now serves the
  request (fail open) rather than 500ing.

## Rollback

- Revert the diff. If a maintenance window is active at rollback time, pair
  the revert with an operator warning: reverting restores both the pool
  coupling on DB-free routes and the sign-in lockout during windows.

## Known caveats

- Cross-worker toggle propagation is bounded by the 5 s cache TTL (same
  process is immediate).
- A mutating request whose cache is cold under pool exhaustion stalls up to
  the pool wait budget (~15 s at defaults) in a worker thread, then fails
  open.
