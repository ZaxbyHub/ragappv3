# 597 — maintenance flag toggles now write the tamper-evident audit row (issue #597)

## What changed

- `backend/app/services/maintenance.py`:
  new frozen `MaintenanceAudit` dataclass (user_id, ip, key_version,
  hmac_sha256, timestamp) and an optional `audit` parameter on
  `MaintenanceService.set_flag`. On a successful version-guarded UPDATE, the
  service INSERTs an `audit_toggle_log` row (feature=`maintenance`, enabled,
  actor, IP, timestamp, key version, HMAC) in the same transaction, so the
  audit row commits atomically with the flag change or rolls back with it.
  Cache invalidation still happens strictly after the successful commit
  (#549 contract), and `set_flag(enabled, reason)` without audit data is
  byte-compatible for existing callers.
- `backend/app/api/routes/admin.py`:
  `POST /admin/maintenance` now computes the HMAC exactly like the generic
  `POST /admin/toggles` route (`_compute_hmac` over
  `feature|enabled|user|ip|timestamp` with the secret manager's key) and
  passes an `MaintenanceAudit` payload to `set_flag`. The route gains the
  `get_secret_manager` dependency; audit-HMAC failures map to 500 before any
  database access, and `sqlite3.Error` maps to 500 with rollback.
- `backend/tests/test_admin_routes_issue273.py`:
  the minimal `_maintenance_app` harness now provides
  `app.state.secret_manager` (as production wiring does), since the route
  resolves it before CSRF/auth can reject.
- New regression coverage: `backend/tests/test_issue597_maintenance_audit.py`
  (audit row per toggle, failed-audit rollback, optimistic-lock retry +
  no partial audit, route roundtrip + cache invalidation, HMAC verification)
  and `backend/tests/test_issue597_guardrail_toggle_audit.py` (AST census:
  every production `set_flag` reference passes the audit payload, `system_flags`
  UPDATE / `audit_toggle_log` INSERT stay in the sanctioned files, and the
  unaudited `ToggleManager.set_toggle` API keeps zero production references).

## Why

Every admin toggle write went through the audited `_write_toggle_with_audit`
path except the maintenance flag: `POST /admin/maintenance` called
`MaintenanceService.set_flag` directly, which performs a bare version-guarded
`system_flags` UPDATE with no `audit_toggle_log` row — no feature record, no
actor, no IP, no HMAC binding. During a maintenance incident (exactly when
the audit trail matters most) the state change was silent and tampering with
it left no evidence. The fix extends the same audit contract to the
`system_flags` writer, keeping the optimistic-lock retry and post-commit
cache invalidation semantics unchanged.

## Migration steps

None. No API removals, schema, config, or wire-format changes;
`audit_toggle_log` already exists in the shipped schema (`init_db`). The
`POST /admin/maintenance` request/response shape is unchanged.

## Known caveats

- The audit row records the acting token as `user_id` (what `require_scope`'s
  auth dict carries in single-admin mode), matching the generic toggle route.
- Audit-INSERT failures surface as HTTP 500 with the flag change rolled back;
  exhausted optimistic-lock retries keep raising `MaintenanceError` with no
  audit row written.
