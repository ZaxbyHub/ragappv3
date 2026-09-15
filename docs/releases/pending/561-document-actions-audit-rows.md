# 561 — Write document-action audit rows on the shipped configuration

**Issue:** #561 (Workstream J, PR 2 of 3; finding C23)
**Rebaseline:** 2026-09-11 · **Roadmap index:** #574

## Outcome

Every document action (upload, read, delete, batch delete, vault-wide delete, retry/reprocess) now writes its `document_actions` audit row on the configuration the repository ships. Previously the sole writer, `_record_document_action`, required `AUDIT_HMAC_KEY_<VERSION>`/`AUDIT_HMAC_KEY` — variables no shipped configuration file ever set — so every audit write raised `SecretManagerError` and was either swallowed into a WARNING (upload/read/delete paths: zero rows written) or escaped as an HTTP 500 after the operation had already succeeded (the `POST /admin/retry/{id}` and retry-chunks endpoints), while `docs/release.md` advertised the feature as shipped.

## What changed

- `backend/app/services/audit_keys.py` (new): one shared fallback derivation — `JWT_SECRET_KEY` → `ADMIN_SECRET_TOKEN` → warn-and-use development constant — moved verbatim in behavior from `security_audit._audit_key`.
- `SecretManager.get_hmac_key` (`backend/app/services/secret_manager.py`): unchanged env precedence (`AUDIT_HMAC_KEY_<VERSION>` → `AUDIT_HMAC_KEY`); when both are unset it now falls back to the shared derivation instead of raising, with one WARNING per process per key version naming the env override. The requested version string is returned unchanged.
- `backend/app/services/security_audit.py`: `_audit_key` is now the shared `derive_audit_key` (same symbol name, byte-identical keys, identical warning text) — the two audit tables (`document_actions`, `security_audit_log`) can no longer diverge on key-availability behavior.
- `.env.example`: documents `AUDIT_HMAC_KEY`/`AUDIT_HMAC_KEY_V1` as commented example entries (same convention as `# AES_KEY=`), including the fallback behavior and the ≥32-byte dedicated-key recommendation.
- `backend/tests/test_issue561_document_actions_shipped_config.py` (new): unmocked end-to-end regression suite — real `SecretManager`, no stubbed key material — covering upload, single delete, batch delete, retry (2xx + row; previously 500), the retry `already_in_progress` branch, versioned-vs-bare key precedence, and explicit-`AUDIT_HMAC_KEY_V1`-wins precedence. Read/download and vault-wide delete share the same choke point and are covered by the existing mocked suites; the three pre-existing audit suites mocked `get_hmac_key`, which is why CI never saw the failure.

## Rollout and rollback

Additive; no schema or HMAC-scheme change (digest remains SHA-256 HMAC over `file_id|action|status|user_id`). Deployments that already set `AUDIT_HMAC_KEY*` are unaffected — the explicit key still wins (pinned by test). Deployments on the fallback key it with `JWT_SECRET_KEY`/`ADMIN_SECRET_TOKEN` material; rotating either secret does not invalidate past rows (digests are never re-verified on read). `POST /admin/toggles` (which reads the same key) stops 500-ing on shipped config as a side effect. Rollback: revert the commit — `document_actions` returns to its previous (empty) state with no data migration in either direction.
