# Audit trail: historical admin-token rows redacted at startup (#894)

Issues: #894 (implemented), #942 and #944 (verify-and-close: both were
already fixed by squash `31197279` / PR #890's feedback round — the collated
trace `.agents/issue-traces/942-944-894-audit-token-redaction` carries the
three-leg ALREADY_FIXED proofs).

## What changed

- New module `backend/app/services/audit_redaction.py` runs at lifespan
  startup, after the secret manager exists (the HMAC key is only reachable
  there), and rewrites every legacy `audit_toggle_log` row whose `user_id`
  still holds raw admin-token material:
  - rows under the CURRENT configured token are re-attributed to the
    non-secret scoped fingerprint (the same identity new rows carry);
  - rows under rotated or otherwise unattributable tokens become the
    explicit tombstone `redacted:legacy-admin-token`. This deliberately
    diverges from the `prompt_versions.created_by` migration (which
    attributes rotated rows to the current fingerprint): `created_by` is an
    authorship field, `audit_toggle_log` is an accountability trail where
    false attribution is worse than a tombstone.
- Every rewritten row's `hmac_sha256` is recomputed over
  `feature|int(enabled)|<new identity>|ip|<original timestamp>` with the
  current key (the production message layout; the original audit timestamp
  is preserved), and `key_version` is updated to the current value — so no
  silently-unverifiable row remains: current-token rows verify, tombstone
  rows self-identify.
- Idempotent and journal-silent on clean databases (issue #699 pattern);
  journals `start`/`succeeded`/`failed` under
  `redact_historical_audit_rows` when work exists.
- Fail-soft by decision: a failed redaction does NOT block boot. It sets
  `app.state.audit_redaction_ok = False`, logs with full traceback, and
  writes a journal `failed` row; detection re-runs on every startup, so the
  next boot retries. The `/api/healthz` payload is deliberately unchanged
  (API surface out of scope) — the observable signals are the app-state
  flag, the ERROR log line, and the migration journal.

## Operator guidance (backups)

Backups created before this change still contain token-material audit rows
at rest — treat them as secret-bearing until the application has booted once
against the restored database: the startup redaction scrubs the live copy on
the first boot. Backup files themselves are not rewritten.

## Known limitations

- A hand-configured legacy admin token that is itself exactly 16 lowercase
  hex characters is indistinguishable from a fingerprint and passes through
  undetected (the configured generator emits 64-char urlsafe tokens — same
  edge the `created_by` migration documents).
- Redaction is irreversible by design (the token material is gone), matching
  the `created_by` migration; rollback of this change restores the behavior
  but not the rewritten rows.
