# Audit trail: historical admin-token rows redacted at startup (#894)

Issues: #894 (implemented), #942 and #944 (verify-and-close: AC1-AC3 of both
were already fixed by squash `31197279` / PR #890's feedback round — the
three-leg ALREADY_FIXED proofs live in the local issue-trace
`.agents/issue-traces/942-944-894-audit-token-redaction`, which is NOT
committed; #942's AC4 — updating the #597 test fixture to pass the identity
value — was implemented by THIS change, in commit 42d4401e).

## What changed

- New module `backend/app/services/audit_redaction.py` runs at lifespan
  startup, after the secret manager exists, and rewrites every legacy
  `audit_toggle_log` row whose `user_id` still holds raw admin-token
  material:
  - rows under the CURRENT configured token are re-attributed to the
    non-secret scoped fingerprint (the same identity new rows carry);
  - rows under rotated or otherwise unattributable tokens become the
    explicit tombstone `redacted:legacy-admin-token`. This deliberately
    diverges from the `prompt_versions.created_by` migration (which
    attributes rotated rows to the current fingerprint): `created_by` is an
    authorship field, `audit_toggle_log` is an accountability trail where
    false attribution is worse than a tombstone.
- A row is only re-attributed when it STILL VERIFIES under the key version
  it was signed with. A row whose stored digest does not verify (it was
  tampered with, or it was signed under a key version that is no longer
  available) becomes a tombstone WITHOUT a valid digest — its secret is
  still scrubbed, and the tombstone states that the row's provenance can no
  longer be established. The redaction therefore never launders a row it
  could not authenticate into a validly-signed entry.
- Rewritten rows get `hmac_sha256` recomputed over
  `feature|int(enabled)|<new identity>|ip|<original timestamp>` with the
  runtime key (the production message layout; the original audit timestamp
  is preserved), and `key_version` set to the key actually used. Rows the
  routine could not verify keep their stored digest and their recorded key
  version.
- Idempotent and journal-silent on clean databases (issue #699 pattern);
  journals `start`/`succeeded`/`failed` under
  `redact_historical_audit_rows` when work exists. The whole data work sits
  inside one guarded region whose handler writes the `failed` row (issue
  #699 AC1) — a lock error, a missing table or a fetch error can no longer
  leave a dangling `start` row.
- Fail-soft by decision: a failed redaction does NOT block boot. It sets
  `app.state.audit_redaction_ok = False` (an in-process diagnostic with no
  production reader) and logs with full traceback; the journal `failed` row
  is the durable signal and the ERROR log line is the immediate one.
  Detection re-runs on every startup, so the next boot retries. The
  `/api/healthz` payload is deliberately unchanged (API surface out of
  scope).

## Operator guidance (backups)

Backups created before this change still contain token-material audit rows
at rest, and the backup FILES are never rewritten. Two separate artifacts
therefore need two separate actions:

1. the RESTORED live copy — the startup redaction scrubs its rows on the
   first boot (boot once against the restored database before treating the
   live copy as clean);
2. the BACKUP FILE itself — it stays secret-bearing indefinitely. The
   durable remedy is to rotate `ADMIN_SECRET_TOKEN` (in single-admin mode
   the token is the sole credential, so rotating it neutralizes the token
   material in every backup) and to delete or re-encrypt old backups under
   your retention policy.

Note also that the redaction rewrites ROWS; SQLite does not compact the
database, so the previous cell contents can linger in the database file's
free space (and in the WAL) until those pages are reused. Treat a
pre-redaction database FILE as secret-bearing until it is vacuumed or
rebuilt.

## Known limitations

- A hand-configured legacy admin token that is itself exactly 16 lowercase
  hex characters is indistinguishable from a fingerprint WHEN IT IS NO
  LONGER THE CONFIGURED TOKEN (a rotated one): the routine matches the
  CURRENT token exactly, so a live 16-hex token IS redacted, but a rotated
  16-hex token passes through undetected and stays at rest (the configured
  generator emits 64-char urlsafe tokens, and `config.py` only requires a
  non-empty token).
- Redaction is irreversible by design (the token material is removed from
  the rows), matching the `created_by` migration; rollback of this change
  restores the behavior but not the rewritten rows.
- Under a concurrent writer the boot-time redaction can wait for the
  database write lock (up to the 30-second migration connect timeout per
  waiting statement) and can then fail softly; the scrub is retried on the
  next boot.
