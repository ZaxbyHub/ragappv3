# Readiness probe reports degraded startup states (Issue #550, Workstream G3)

## What changed

### Backend — `/api/healthz` readiness signals
- `GET /api/healthz` now surfaces the degraded states startup already logs, in
  addition to the three attribute-presence checks it already had:
  - **Failed startup migration** → 503 with issue `database migration failed at
    startup`. Lifespan records the outcome of its `run_migrations` call on
    `app.state.migrations_ok` (in-memory; no per-request DB query — preserves the
    issue #549 C02 fix that keeps synchronous pooled reads off the event loop).
  - **Vector-store model mismatch** (`vector_store._ready == False`) → 503 with
    issue `vector_store not ready (embedding model mismatch - reindex required)`.
  - **Pool saturation** → 503 with issue `db pool saturated: recent connection
    wait`. `SQLiteConnectionPool` now records a lock-guarded monotonic timestamp
    whenever a checkout is forced to wait for a connection (the at-capacity
    blocking-wait branch); the probe reports saturation if such a wait happened
    within the last 30 seconds (`CAPACITY_WAIT_WINDOW_SECONDS`, one compose
    healthcheck interval). A fully-allocated but idle pool is the normal warm
    steady state and does NOT report.
  - **Maintenance mode** → reported as a NON-blocking entry under a new
    `warnings` list (status stays 200): `maintenance mode enabled`. The
    operator-set reason string is deliberately NOT included — this probe is
    unauthenticated (and compose binds it on 0.0.0.0), so operator free text
    stays behind the admin-authed `GET /api/admin/maintenance` (PR #600 review
    PRR-001). Maintenance is an intentional state, not a fault; the flag is
    read via the cached off-loop `MaintenanceService.get_flag_async()` path.
- Response shapes are additive: the healthy body is still exactly
  `{"status": "ok"}`; degraded is still 503 `{"status": "degraded", "issues":
  [...]}`; the `warnings` key appears only when there is something to report
  (and is included alongside `issues` on a 503 so operators see maintenance
  windows during unrelated degradation).
- `docker-compose.yml`'s backend healthcheck now polls `/api/healthz` instead of
  `/health`. `/health` (`{"status": "ok"}` literal) remains pure liveness and is
  unchanged. The frontend services' own healthchecks are untouched.

### Tests
- New `backend/tests/test_health_readiness.py`: frozen acceptance checks for all
  four signals through the real HTTP route (real pool + real `MaintenanceService`
  on temp DBs; strict `app.state` snapshot/restore isolation), a source-level C02
  guard asserting `healthz` performs no pool checkout / raw flag read / raw SQL,
  a compose-target check, a no-telemetry-dependency guard, and healthy-state
  preservation of the existing three checks.
- New `backend/tests/test_health_readiness_lifespan.py`: source-wiring guard that
  lifespan records `migrations_ok` around its `run_migrations` call and that
  `healthz` reads it.

## Why

Startup already logs these degraded states (a failed migration is swallowed with
"app will start with degraded database state"; a vector-store embedding-model
mismatch logs "VECTOR STORE MODEL MISMATCH"; pool exhaustion logs
`pool_exhausted`), but the readiness probe only checked attribute presence, and
the container healthcheck polled a route that cannot fail — so orchestrator
readiness gates were inert against exactly the states that make the app unable
to serve. Audit findings C07 and E03 (readiness half), Workstream G3.

## Operator-visible outcomes and rollout

- An orchestrator with `restart: unless-stopped`, or any Kubernetes/Nomad
  readiness gate, pointed at the backend healthcheck can now observe failures it
  previously never saw: during a genuinely failed startup migration, a
  vector-store model mismatch (until a reindex completes), or within 30s of a
  pool capacity wait. This is the intended behavior change — verify your
  restart policies before deploying. Under compose defaults (interval 30s ×
  retries 3) a persistently failing readiness check restarts the container
  roughly every ~90 seconds; there is no additional backoff, by design — the
  container is genuinely unable to serve.
- A maintenance window does NOT fail the probe (the flag is reported, not
  blocking), so enabling maintenance will not restart the container via this
  healthcheck.
- The deferred fifth readiness component ("last provider probe") remains with
  #494/#551; the telemetry half of E03 (OpenTelemetry/Prometheus) remains with
  #518. No new third-party dependency is introduced.

## Rollback

Revert the `healthz` handler, the two `migrations_ok` lines in `lifespan.py`,
the pool capacity-wait instrumentation, and the compose probe URL (back to
`/health`). No schema or data migration is involved.
