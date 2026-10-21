"""Startup redaction of historical admin-token audit rows (issue #894).

Historical ``audit_toggle_log`` rows written before the #709 class-(a) fix
(squash ``31197279`` / PR #890) keep RAW admin bearer-token material in the
readable ``user_id`` column forever: the fingerprint fix only changed what
NEW rows carry, and the ``migrate_redact_prompt_created_by`` migration
deliberately excluded this table because each row's ``hmac_sha256`` covers
``user_id`` (rewriting it in place invalidates the digest) and the HMAC key
is resolved from the environment rather than from settings. This module runs
at lifespan time, once the secret manager exists, and rewrites every legacy
token row with an integrity story:

* a row that still verifies under its recorded ``key_version`` is
  re-attributed (current token -> the scoped fingerprint; anything else ->
  the ``redacted:legacy-admin-token`` tombstone) and re-signed under the
  runtime key, so it keeps verifying;
* a row that does NOT verify (its digest was tampered with, or it was
  signed under a key version that is no longer available) becomes a
  tombstone WITHOUT a valid digest — the secret is still scrubbed, and the
  tombstone marker states that the row's provenance can no longer be
  established.

Because detection runs on every startup, a backup restored from a pre-fix
era is scrubbed on the next boot. The routine is fail-soft: a failure is
logged and the boot continues, because blocking a deployment over
historical-row hygiene is worse than deferring the scrub to the next boot.
"""

import hashlib
import hmac
import logging
import sqlite3

from app.config import settings
from app.models.migration_journal import (
    MIGRATION_CONNECT_TIMEOUT_SECONDS,
    record_migration_outcome,
)
from app.security import admin_scope_fingerprint

logger = logging.getLogger(__name__)

# Explicit tombstone for rows whose actor token cannot be attributed (a
# rotated or otherwise unknown legacy token) and for rows whose stored
# digest no longer verifies. The "redact" marker is what lets a reader
# recognize an honest redaction rather than a silently unverifiable row
# (no production reader consumes the table today, so the marker is a
# convention for operators and future verifiers); the value is deliberately
# NOT a fingerprint so it can never be mistaken for an actor identity.
REDACTED_LEGACY_IDENTITY = "redacted:legacy-admin-token"

# A fingerprint is exactly 16 lowercase hex chars (sha256 hexdigest[:16]).
# EXACT shape of migrate_redact_prompt_created_by's ``fingerprint_glob``
# (backend/app/models/database.py:7536) — anything else non-empty is
# legacy token material by elimination.
_FINGERPRINT_GLOB = "[0-9a-f]" * 16

_MIGRATION_NAME = "redact_historical_audit_rows"

# The detection predicate mirrors the sibling migration's dual predicate:
# the EXACT current token is always selected (it is known at runtime, so it
# can never be "indistinguishable from a fingerprint"), and everything
# else that is neither empty nor a 16-hex fingerprint nor a tombstone is
# selected by elimination (a rotated or unknown legacy token).
# Two literal statements instead of a composed one: the shape is fixed, and
# no SQL text is ever built from data.
_DETECT_SQL_WITH_TOKEN = (  # nosec B105 - SQL predicate constant; the name says which column it inspects, it holds no credential
    "SELECT 1 FROM audit_toggle_log WHERE "
    "(user_id IS NOT NULL AND user_id != '' AND user_id NOT GLOB ? "
    "AND user_id != ?) OR user_id = ? LIMIT 1"
)
_DETECT_SQL_NO_TOKEN = (  # nosec B105 - SQL predicate constant; the name says which column it inspects, it holds no credential
    "SELECT 1 FROM audit_toggle_log WHERE "
    "user_id IS NOT NULL AND user_id != '' AND user_id NOT GLOB ? "
    "AND user_id != ? LIMIT 1"
)
_ROWS_SQL_WITH_TOKEN = (  # nosec B105 - SQL predicate constant; the name says which column it inspects, it holds no credential
    "SELECT id, feature, enabled, user_id, ip, timestamp, hmac_sha256, "
    "key_version FROM audit_toggle_log WHERE "
    "(user_id IS NOT NULL AND user_id != '' AND user_id NOT GLOB ? "
    "AND user_id != ?) OR user_id = ?"
)
_ROWS_SQL_NO_TOKEN = (  # nosec B105 - SQL predicate constant; the name says which column it inspects, it holds no credential
    "SELECT id, feature, enabled, user_id, ip, timestamp, hmac_sha256, "
    "key_version FROM audit_toggle_log WHERE "
    "user_id IS NOT NULL AND user_id != '' AND user_id NOT GLOB ? "
    "AND user_id != ?"
)


def _audit_message(
    feature: str, enabled: bool, user_id: str | None, ip: str | None, timestamp: str
) -> str:
    """Byte-exact replica of ``app.api.routes.admin._compute_hmac``'s message.

    The ``or ''`` fallbacks are load-bearing (real rows can carry
    ``ip IS NULL``); a ``str(None)``-style formatting bug would produce
    digests that never verify.
    """
    return f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"


def _digest(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _configured_token() -> str:
    return getattr(settings, "admin_secret_token", "") or ""


def historical_audit_rows_pending(sqlite_path: str) -> bool:
    """True when at least one row still carries legacy token material.

    Used by the lifespan wrapper to resolve the HMAC key ONLY when there is
    work to do: resolving the key on every clean boot emits a
    warn-and-fallback log line when no dedicated audit key is configured.
    """
    token = _configured_token()
    conn = sqlite3.connect(
        sqlite_path,
        timeout=MIGRATION_CONNECT_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        if token:
            found = conn.execute(
                _DETECT_SQL_WITH_TOKEN,
                (_FINGERPRINT_GLOB, REDACTED_LEGACY_IDENTITY, token),
            ).fetchone()
        else:
            found = conn.execute(
                _DETECT_SQL_NO_TOKEN,
                (_FINGERPRINT_GLOB, REDACTED_LEGACY_IDENTITY),
            ).fetchone()
        return found is not None
    finally:
        conn.close()


def redact_historical_audit_rows(
    sqlite_path: str, hmac_key: bytes, key_version: str
) -> int:
    """Rewrite legacy token-material ``audit_toggle_log`` rows; return count.

    Rows needing redaction: ``user_id`` equal to the configured token, or
    non-empty and neither a 16-hex fingerprint nor the tombstone literal
    (a rotated or otherwise unknown legacy token). The tombstone exclusion
    is an EXACT literal — a marker-substring exclusion would let a rotated
    token containing "redact" survive un-redacted. Known edge, mirroring the
    #944 migration's documented limitation: a hand-configured legacy token
    that is itself exactly 16 lowercase hex chars is indistinguishable
    from a fingerprint when it is UNKNOWN (rotated); the CURRENT token is
    always matched exactly, so it is never skipped.

    Every rewritten row gets a digest recomputed with the passed
    ``hmac_key`` over the production message layout with its ORIGINAL
    timestamp preserved, and ``key_version`` set to the passed value. A row
    whose stored digest does not verify under its recorded ``key_version``
    is tombstoned WITHOUT a valid digest (its provenance can no longer be
    established) rather than re-attributed.

    Idempotent; journal-silent on clean databases (issue #699 pattern); the
    whole data work sits inside ONE try whose handler writes the ``failed``
    journal row (issue #699 AC1) and re-raises.
    """
    token = _configured_token()
    fingerprint = admin_scope_fingerprint(token) if token else None
    conn = sqlite3.connect(
        sqlite_path,
        timeout=MIGRATION_CONNECT_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        if token:
            work_remains = conn.execute(
                _DETECT_SQL_WITH_TOKEN,
                (_FINGERPRINT_GLOB, REDACTED_LEGACY_IDENTITY, token),
            ).fetchone()
        else:
            work_remains = conn.execute(
                _DETECT_SQL_NO_TOKEN,
                (_FINGERPRINT_GLOB, REDACTED_LEGACY_IDENTITY),
            ).fetchone()
        if work_remains is None:
            # Journal-silent no-op path (issue #699 AC2): clean boots and
            # already-redacted databases write no rows.
            return 0
        record_migration_outcome(
            conn,
            migration_name=_MIGRATION_NAME,
            phase="start",
            outcome="ok",
        )
        conn.execute("BEGIN IMMEDIATE")
        if token:
            rows = conn.execute(
                _ROWS_SQL_WITH_TOKEN,
                (_FINGERPRINT_GLOB, REDACTED_LEGACY_IDENTITY, token),
            ).fetchall()
        else:
            rows = conn.execute(
                _ROWS_SQL_NO_TOKEN,
                (_FINGERPRINT_GLOB, REDACTED_LEGACY_IDENTITY),
            ).fetchall()
        redacted = 0
        for (
            row_id,
            feature,
            enabled,
            user_id,
            ip,
            timestamp,
            stored_digest,
            stored_key_version,
        ) in rows:
            # A row is only re-attributed when it still verifies under the
            # key version it was signed with. A row signed under a rotated
            # key cannot be verified at all, and a tampered row fails its
            # digest: both become tombstones (the secret is still scrubbed)
            # rather than a re-attributed, now-verifying row.
            verified = False
            if stored_key_version == key_version:
                expected = _digest(
                    hmac_key,
                    _audit_message(feature, bool(enabled), user_id, ip, timestamp),
                )
                verified = hmac.compare_digest(expected, stored_digest or "")
            if verified:
                if token and user_id == token and fingerprint is not None:
                    new_identity = fingerprint
                else:
                    new_identity = REDACTED_LEGACY_IDENTITY
                new_digest = _digest(
                    hmac_key,
                    _audit_message(
                        feature, bool(enabled), new_identity, ip, timestamp
                    ),
                )
                new_key_version = key_version
            else:
                new_identity = REDACTED_LEGACY_IDENTITY
                new_digest = stored_digest
                new_key_version = stored_key_version
            cur = conn.execute(
                "UPDATE audit_toggle_log "
                "SET user_id = ?, hmac_sha256 = ?, key_version = ? "
                "WHERE id = ? AND user_id = ?",
                (new_identity, new_digest, new_key_version, row_id, user_id),
            )
            if cur.rowcount:
                redacted += 1
        conn.execute("COMMIT")
        if redacted:
            logger.info(
                "Redacted %d historical audit_toggle_log row(s) that held "
                "admin token material (issue #894).",
                redacted,
            )
        record_migration_outcome(
            conn,
            migration_name=_MIGRATION_NAME,
            phase="succeeded",
            outcome="ok",
            detail=f"redacted_rows={redacted}",
        )
        return redacted
    except Exception as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rollback_exc:  # pragma: no cover
                logger.warning(
                    "audit_redaction: ROLLBACK failed: %s", rollback_exc
                )
        record_migration_outcome(
            conn,
            migration_name=_MIGRATION_NAME,
            phase="failed",
            outcome="error",
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        conn.close()


def run_startup_audit_redaction(app) -> None:
    """Lifespan wiring: scrub historical audit rows once the key exists.

    Called from ``app/lifespan.py`` after ``app.state.secret_manager`` is
    created, on a worker thread. Fail-soft by decision (issue #894): a
    failed redaction must not block boot — it flags
    ``app.state.audit_redaction_ok`` False and logs with full traceback;
    detection re-runs on every startup so the next boot retries. The flag
    is an in-process diagnostic (no production reader reads it, and the
    health payload is deliberately unchanged); the operator-visible signals
    are the ERROR log line and the journal ``failed`` row.
    """
    app.state.audit_redaction_ok = True
    try:
        sqlite_path = str(settings.sqlite_path)
        if not historical_audit_rows_pending(sqlite_path):
            # No work: do NOT resolve the HMAC key (resolving it on every
            # clean boot emits a warn-and-fallback log line when no
            # dedicated audit key is configured).
            return
        key, key_version = app.state.secret_manager.get_hmac_key()
        # The routine logs the redaction count itself; do not log it twice.
        redact_historical_audit_rows(sqlite_path, key, key_version)
    except Exception:
        app.state.audit_redaction_ok = False
        logger.exception(
            "Startup audit-row redaction failed (issue #894): historical "
            "rows may still hold admin token material at rest; the redaction "
            "will be retried on the next boot."
        )
