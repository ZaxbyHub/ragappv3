# Audit/auth/boot follow-ups: audit-key derivation, credential comparison, CI test isolation (#993, #987, #995)

Issues: #993 and #995 (follow-ups from PR #989) and #987 (follow-up from
PR #986). Collated per maintainer instruction; each issue keeps its
full-resolution contract inside this change.

## What changed

### #993 — audit HMAC key no longer falls back to the public placeholder

`derive_audit_key()` (the single derivation shared by the
`document_actions` audit path via `SecretManager.get_hmac_key` and
`security_audit_log` via `security_audit._audit_key`) now treats the
well-known insecure default `JWT_SECRET_KEY` as unset. In single-admin
deployments (`USERS_ENABLED=false`) with a configured `ADMIN_SECRET_TOKEN`
and no changed `JWT_SECRET_KEY`, the audit HMAC key was previously the
public constant `change-me-to-a-random-64-char-string` — anyone could forge
verifying audit rows. The key now falls through to the configured admin
token. A genuinely-set JWT secret stays preferred; the
warn-and-use-development-constant path for deployments with neither secret
is unchanged.

Key-rotation consequence (disclosed precisely): audit rows previously
signed under the placeholder stop verifying under the new key. The #894
startup redaction tombstones exactly the legacy-token-material
`audit_toggle_log` rows its predicate selects (their secret is still
scrubbed and the tombstone states that provenance can no longer be
established). Placeholder-signed `document_actions` and
`security_audit_log` rows have no tombstone path and keep
silently-unverifiable digests; already-fingerprinted `audit_toggle_log`
rows are not revisited. Pinning keys explicitly is table-scoped: a dedicated
`AUDIT_HMAC_KEY` pins the `document_actions` and `audit_toggle_log` keys
(both write paths resolve their key through `SecretManager.get_hmac_key`,
which reads the env var), while `security_audit_log` always uses the shared
derivation — pin that one by setting a changed `JWT_SECRET_KEY` (or the
admin token); a changed `JWT_SECRET_KEY` pins all three. The digest covers
the row's plaintext columns (e.g.
`feature|int(enabled)|user_id|ip|timestamp` for `audit_toggle_log`), so a
verifier holding the key can recompute it offline; the message layouts are
documented in the admin guide (`security_audit_log`) and in the #561 /
#597 release fragments (`document_actions`, `audit_toggle_log`).

### #993 — non-ASCII credentials no longer turn 401/403 into 500

`secrets.compare_digest`/`hmac.compare_digest` raise `TypeError` when
comparing `str` values containing non-ASCII characters, so a bearer value
or `X-API-Key` carrying non-ASCII bytes (legal on the wire) crashed the
auth dependency with an unhandled 500. A shared
`app.security.constant_time_equals` compares utf-8 bytes (total) and fails
closed on non-`str` input (a forged non-string `fpt` JWT claim — a
`TypeError` on the raw compare at base — now maps to the route's
invalid-credential status; unencodable lone-surrogate strings fail closed
too). Adopted at every credential-comparison
site (`require_auth`, `require_scope`, `_resolve_active_user` x2, the JWT
fingerprint check, `require_health_probe_auth`). ASCII inputs keep their
exact previous 401/403 statuses.

### #995 — startup audit redaction takes the write lock once

`redact_historical_audit_rows` now executes `BEGIN IMMEDIATE` before any
journal write, journals `start` inside that transaction, wraps the data
work in a `SAVEPOINT`, and funnels every failure into one handler that
writes exactly one terminal `failed` row on a bounded best-effort budget
(`PRAGMA busy_timeout` lowered to one tenth of the connect timeout, floor
50 ms). A continuously held write lock costs one connect timeout plus the
bounded write (measured worst case ~36 s) instead of three sequential
timeouts (~87 s). The #699 journal contract (terminal `failed` row, data
rolled back, journal-silent clean boots) is preserved; see the #894
fragment for the aggregate-bound wording. A failing `BEGIN IMMEDIATE`
now journals exactly `[failed]` — `start` is only durable once the lock is
held.

### #987 — settings-singleton test pollution (Backend CI red)

`_O04cBase` (backend/tests/test_o04c_feedback_pins.py) now snapshots and
restores all seven `wiki_llm_curator_*` attributes its tests assign
(previously only `wiki_llm_curator_enabled` was restored, so the leaked
`url`/`model` values flipped
`test_settings_curator.py::test_put_rejects_curator_enabled_without_url_or_model`
from 422 to 200 whenever xdist co-scheduled the two files on one worker —
the Backend-CI failure drawn on PRs #986 and #989). The same-class leak in
`test_wiki_curator.py` (`wiki_llm_curator_concurrency`, found by the new
census) is restored too. New guardrails: an in-process co-run pollution
guard and a repo-wide census
(`backend/tests/test_settings_singleton_census.py`, 1043 assignments
scanned) that fails on any un-restored settings-singleton assignment.

## Operator guidance

- Single-admin deployments that ran with the placeholder JWT secret should
  set a changed `JWT_SECRET_KEY` (which pins the shared derivation for all
  three audit tables) or a dedicated `AUDIT_HMAC_KEY` (`document_actions`
  and `audit_toggle_log`; `security_audit_log` stays on the shared
  derivation), and treat pre-existing audit digests as unverifiable going
  forward (see the rotation scope above).
- Known residual (ASK, maintainer decision): if `ADMIN_SECRET_TOKEN` is
  itself the well-known default `admin-secret-token` (already
  CRITICAL-warned at startup), the derived audit key is still a public
  constant; refusing that default in single-admin mode is a config-policy
  change tracked for the maintainer's call.
