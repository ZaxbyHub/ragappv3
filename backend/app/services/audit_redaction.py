"""Startup redaction of historical admin-token audit rows (issue #894).

Historical ``audit_toggle_log`` rows written before the #709 class-(a) fix
(squash ``31197279`` / PR #890) keep RAW admin bearer-token material in the
readable ``user_id`` column forever: the fingerprint fix only changed what
NEW rows carry, and the ``migrate_redact_prompt_created_by`` migration
deliberately excluded this table because each row's ``hmac_sha256`` covers
``user_id`` (rewriting it in place invalidates the digest) and the HMAC key
is only reachable in production through the secret manager, which plain
schema migrations do not have. This module runs at lifespan time, after the
secret manager exists, and rewrites every legacy token row with an integrity
story: rows under the CURRENT configured token are re-attributed to the
non-secret scoped fingerprint with a recomputed verifying digest; rows under
rotated or otherwise unattributable tokens become an explicit tombstone
identity, also with a recomputed digest. Because detection runs on every
startup, a backup restored from a pre-fix era is scrubbed on the next boot.
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
# rotated or otherwise unknown legacy token). The marker "redact" is what
# integrity checks recognize as an honest redaction rather than a
# silently-unverifiable row; the value is deliberately NOT a fingerprint so
# it can never be mistaken for an actor identity.
REDACTED_LEGACY_IDENTITY = "redacted:legacy-admin-token"

# A fingerprint is exactly 16 lowercase hex chars (sha256 hexdigest[:16]).
# EXACT shape of migrate_redact_prompt_created_by's ``fingerprint_glob``
# (backend/app/models/database.py:7536) — anything else non-empty in
# ``user_id`` is legacy token material by elimination, including rows
# written under previously rotated tokens.
_FINGERPRINT_GLOB = "[0-9a-f]" * 16

_MIGRATION_NAME = "redact_historical_audit_rows"


def _audit_message(
    feature: str, enabled: bool, user_id: str | None, ip: str | None, timestamp: str
) -> str:
    """Byte-exact replica of ``app.api.routes.admin._compute_hmac``'s message.

    The ``or ''`` fallbacks are load-bearing (real rows can carry
    ``ip IS NULL``); a ``str(None)``-style formatting bug would produce
    digests that never verify.
    """
    return f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"


def redact_historical_audit_rows(
    sqlite_path: str, hmac_key: bytes, key_version: str
) -> int:
    """Rewrite legacy token-material ``audit_toggle_log`` rows; return count.

    Rows needing redaction: ``user_id`` non-NULL, non-empty, not a 16-hex
    fingerprint, and not already the tombstone literal (exact-literal
    exclusion keeps re-runs no-ops; a marker-substring exclusion would let a
    rotated token containing "redact" survive un-redacted). Known edge,
    mirroring the #944 migration's documented limitation: a hand-configured
    legacy token that is itself exactly 16 lowercase hex chars is
    indistinguishable from a fingerprint and passes through (the configured
    generator emits 64-char urlsafe tokens).

    Every rewritten row gets a digest recomputed with ``hmac_key`` over the
    production message layout with its ORIGINAL timestamp preserved, and
    ``key_version`` is set to the passed value (truthful: the digest is now
    computed under the current key). Idempotent; journal-silent on clean
    databases (issue #699 pattern); journals start/succeeded/failed outcomes
    on autocommit, outside the data transaction, so a failed attempt still
    records its failure.
    """
    token = getattr(settings, "admin_secret_token", "") or ""
    fingerprint = admin_scope_fingerprint(token) if token else None
    conn = sqlite3.connect(
        sqlite_path,
        timeout=MIGRATION_CONNECT_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        work_remains = conn.execute(
            "SELECT 1 FROM audit_toggle_log "
            "WHERE user_id IS NOT NULL AND user_id != '' "
            "AND user_id NOT GLOB ? AND user_id != ? LIMIT 1",
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
        rows = conn.execute(
            "SELECT id, feature, enabled, user_id, ip, timestamp "
            "FROM audit_toggle_log "
            "WHERE user_id IS NOT NULL AND user_id != '' "
            "AND user_id NOT GLOB ? AND user_id != ?",
            (_FINGERPRINT_GLOB, REDACTED_LEGACY_IDENTITY),
        ).fetchall()
        redacted = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row_id, feature, enabled, user_id, ip, timestamp in rows:
                if token and user_id == token and fingerprint is not None:
                    new_identity: str = fingerprint
                else:
                    new_identity = REDACTED_LEGACY_IDENTITY
                digest = hmac.new(
                    hmac_key,
                    _audit_message(feature, bool(enabled), new_identity, ip, timestamp).encode(
                        "utf-8"
                    ),
                    hashlib.sha256,
                ).hexdigest()
                conn.execute(
                    "UPDATE audit_toggle_log "
                    "SET user_id = ?, hmac_sha256 = ?, key_version = ? "
                    "WHERE id = ?",
                    (new_identity, digest, key_version, row_id),
                )
                redacted += 1
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            record_migration_outcome(
                conn,
                migration_name=_MIGRATION_NAME,
                phase="failed",
                outcome="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        record_migration_outcome(
            conn,
            migration_name=_MIGRATION_NAME,
            phase="succeeded",
            outcome="ok",
            detail=f"redacted_rows={redacted}",
        )
        return redacted
    finally:
        conn.close()


def run_startup_audit_redaction(app) -> None:
    """Lifespan wiring: scrub historical audit rows once the key exists.

    Called from ``app/lifespan.py`` after ``app.state.secret_manager`` is
    created, on a worker thread. Fail-soft by decision (plan Decision, issue
    #894): a failed redaction must not block boot — it flags
    ``app.state.audit_redaction_ok`` False and logs with full traceback;
    detection re-runs on every startup so the next boot retries. The flag
    mirrors ``app.state.migrations_ok`` at the app-state level; the health
    payload is deliberately not extended (API surface out of scope), so the
    observable signals are this flag, the ERROR log line, and the journal
    ``failed`` row.
    """
    app.state.audit_redaction_ok = True
    try:
        key, key_version = app.state.secret_manager.get_hmac_key()
        redacted = redact_historical_audit_rows(
            str(settings.sqlite_path), key, key_version
        )
        if redacted:
            logger.info(
                "Redacted %d historical audit_toggle_log row(s) that held "
                "admin token material (issue #894).",
                redacted,
            )
    except Exception:
        app.state.audit_redaction_ok = False
        logger.exception(
            "Startup audit-row redaction failed (issue #894): historical "
            "rows may still hold admin token material at rest; the redaction "
            "will be retried on the next boot."
        )
