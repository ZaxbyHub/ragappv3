"""Acceptance checks for the #894 startup redaction of historical audit rows.

Checks C8/C9 of the 942-944-894 trace. Historical ``audit_toggle_log`` rows
written before the fingerprint fix keep RAW admin bearer-token material in
the readable ``user_id`` column forever; the fix exposes a module-level
startup-redaction routine in a NEW module
``app.services.audit_redaction``::

    redact_historical_audit_rows(sqlite_path: str, hmac_key: bytes,
                                 key_version: str) -> int

returning the number of rows redacted, idempotent, and called from the
lifespan after secret-manager creation (the HMAC key is only reachable via
the secret manager, so ``run_migrations`` cannot do this).

These are NEW-SURFACE checks: RED at base (the module
``app.services.audit_redaction`` does not exist, so this file fails at
import/collection), GREEN after the fix.

Because each row's ``hmac_sha256`` is computed over
``feature|int(enabled)|user_id|ip|timestamp``, the seeded rows carry digests
computed with the REAL ``_compute_hmac`` from ``app.api.routes.admin`` so the
integrity assertions below are meaningful. Both a row under the CURRENT
configured token and a row under a rotated token are seeded; a
control row already holding the fingerprint must pass through untouched.
Fingerprints are always computed with BOTH ``settings.admin_secret_token``
and ``settings.admin_token_scopes`` patched, because
``admin_scope_fingerprint`` reads the scope map from settings.
"""

import hashlib
import hmac
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.api.routes.admin import _compute_hmac
from app.config import settings
from app.models.database import run_migrations
from app.security import admin_scope_fingerprint
from app.services.audit_redaction import redact_historical_audit_rows

CURRENT_TOKEN = "audit-redaction-current-token"
ROTATED_TOKEN = "audit-redaction-rotated-token-7c3d9e"
TEST_KEY = b"audit-redaction-test-key"
TEST_KEY_VERSION = "v-audit-redaction"
SEED_IP = "10.9.8.7"

CURRENT_ROW_FEATURE = "issue894_current"
ROTATED_ROW_FEATURE = "issue894_rotated"
CONTROL_FEATURE = "issue894_control"

# A tombstone is a redacted row that explicitly identifies itself as
# redacted via its own user_id carrying the marker "redact"
# (case-insensitive) — e.g. "REDACTED" or "redacted:<fingerprint>". A row
# whose digest no longer verifies AND whose user_id carries no marker is
# silently unverifiable and fails C9.
_TOMBSTONE_MARKER = "redact"


@contextmanager
def _temp_redaction_db():
    """Yield a migrated db path; always drop cached pools and remove the dir."""
    temp_dir = tempfile.mkdtemp(prefix="audit_redaction_")
    db_path = str(Path(temp_dir) / "app.db")
    try:
        run_migrations(db_path)
        yield db_path
    finally:
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            pool = _pool_cache.pop(db_path, None)
        if pool is not None:
            pool.close_all()
        shutil.rmtree(temp_dir, ignore_errors=True)


@contextmanager
def _configured_admin():
    """Patch BOTH token settings so the current token is deterministic."""
    with patch.object(settings, "admin_secret_token", CURRENT_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {CURRENT_TOKEN: ["admin:config"]}
        ):
            yield


def _insert_signed_row(
    conn: sqlite3.Connection,
    feature: str,
    enabled: bool,
    user_id: str,
) -> None:
    """Insert one audit row whose digest is computed with the REAL helper."""
    digest, timestamp = _compute_hmac(TEST_KEY, feature, enabled, user_id, SEED_IP)
    conn.execute(
        "INSERT INTO audit_toggle_log"
        "(feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (feature, int(enabled), user_id, SEED_IP, timestamp, TEST_KEY_VERSION, digest),
    )


def _snapshot_conn(conn: sqlite3.Connection) -> dict[str, tuple]:
    rows = conn.execute(
        "SELECT feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256 "
        "FROM audit_toggle_log ORDER BY id"
    ).fetchall()
    return {row[0]: tuple(row) for row in rows}


def _snapshot(db_path: str) -> dict[str, tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return _snapshot_conn(conn)
    finally:
        conn.close()


def _seed_historical_rows(db_path: str) -> dict[str, tuple]:
    """Seed the #894 before-state and return a snapshot keyed by feature.

    One row under the CURRENT configured token, one under a rotated token,
    and one control row already holding the fingerprint (computed under the
    same patched settings the redaction call will run under).
    """
    conn = sqlite3.connect(db_path)
    try:
        with _configured_admin():
            control_identity = admin_scope_fingerprint(CURRENT_TOKEN)
            _insert_signed_row(conn, CURRENT_ROW_FEATURE, True, CURRENT_TOKEN)
            _insert_signed_row(conn, ROTATED_ROW_FEATURE, False, ROTATED_TOKEN)
            _insert_signed_row(conn, CONTROL_FEATURE, True, control_identity)
        conn.commit()
        return _snapshot_conn(conn)
    finally:
        conn.close()


def _digest_verifies(row: tuple) -> bool:
    """True iff the row verifies under the production HMAC scheme.

    Recomputes the digest over the row's CURRENT (redacted) identity with
    the key the redaction routine was given — exactly the message layout of
    ``_compute_hmac`` / test_issue597's AC5 verifier.
    """
    feature, enabled, user_id, ip, timestamp, _key_version, digest = row
    message = f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"
    expected = hmac.new(
        TEST_KEY, message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, digest)


def _is_tombstone(row: tuple) -> bool:
    """True iff the row explicitly identifies itself as a redaction tombstone."""
    user_id = row[2]
    return user_id is not None and _TOMBSTONE_MARKER in str(user_id).lower()


def test_historical_token_rows_redacted():
    """C8/AC8: the startup redaction rewrites historical token rows.

    RED at base (module app.services.audit_redaction is absent, so this file
    fails at collection), GREEN after the fix. After
    ``redact_historical_audit_rows`` runs on a DB holding one row under the
    CURRENT configured token and one under a rotated token, no
    audit_toggle_log row's user_id equals the configured admin token (nor
    the rotated one); the already-fingerprinted control row passes through
    byte-identical; rows survive (no DELETE-based pseudo-remediation); and a
    second run is a no-op (returns 0, changes nothing).
    """
    with _temp_redaction_db() as db_path:
        before = _seed_historical_rows(db_path)
        # Sanity: the seeded before-state really holds raw token material.
        assert before[CURRENT_ROW_FEATURE][2] == CURRENT_TOKEN
        assert before[ROTATED_ROW_FEATURE][2] == ROTATED_TOKEN

        with _configured_admin():
            first = redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
            after_first = _snapshot(db_path)
            second = redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
            after_second = _snapshot(db_path)

        assert isinstance(first, int), (
            "redact_historical_audit_rows must return the number of rows "
            f"redacted as an int, got {type(first).__name__}"
        )
        assert first >= 2, (
            "both seeded historical token rows (current + rotated) must be "
            f"counted as redacted, got {first}"
        )
        user_ids = [row[2] for row in after_first.values()]
        assert CURRENT_TOKEN not in user_ids, (
            "after startup redaction no audit_toggle_log row's user_id may "
            "equal the configured admin token"
        )
        assert ROTATED_TOKEN not in user_ids, (
            "after startup redaction no audit_toggle_log row's user_id may "
            "equal a rotated admin token"
        )
        # Rows survive and the control row is untouched.
        assert set(after_first) >= {
            CURRENT_ROW_FEATURE,
            ROTATED_ROW_FEATURE,
            CONTROL_FEATURE,
        }, "redaction must rewrite rows in place, not delete them"
        assert after_first[CONTROL_FEATURE] == before[CONTROL_FEATURE], (
            "the already-fingerprinted control row must pass through "
            "unchanged (value, digest and all)"
        )
        # Idempotent: a second run redacts nothing and changes nothing.
        assert second == 0, f"idempotent re-run must redact 0 rows, got {second}"
        assert after_second == after_first, (
            "idempotent re-run must leave every audit row byte-identical"
        )


def test_redacted_rows_have_integrity_story():
    """C9/AC9: every redacted row either verifies or is an explicit tombstone.

    RED at base (module app.services.audit_redaction is absent, so this file
    fails at collection), GREEN after the fix. Rewriting user_id invalidates
    the digest computed over the original identity, so each redacted row
    needs an integrity story: either its stored hmac_sha256 verifies under
    the production HMAC scheme recomputed over its redacted identity, or the
    row explicitly identifies itself as a redaction tombstone (user_id
    contains the marker "redact", case-insensitive). A row that neither
    verifies nor self-identifies is silently unverifiable and fails.
    """
    with _temp_redaction_db() as db_path:
        _seed_historical_rows(db_path)
        with _configured_admin():
            redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        rows = _snapshot(db_path)

        # Non-vacuous: all seeded rows are still present for judgement.
        for feature in (CURRENT_ROW_FEATURE, ROTATED_ROW_FEATURE, CONTROL_FEATURE):
            assert feature in rows, f"seeded row {feature} disappeared"

        for feature, row in sorted(rows.items()):
            verifies = _digest_verifies(row)
            tombstone = _is_tombstone(row)
            assert verifies or tombstone, (
                f"audit row {feature!r} is silently unverifiable after "
                f"redaction: its hmac_sha256 does not verify over its "
                f"redacted identity (user_id={row[2]!r}) and the row carries "
                "no explicit redaction marker — redacted rows must either "
                "carry a recomputed digest or an explicit tombstone identity"
            )

        # The untouched control row was production-verifiable before and
        # must still verify afterwards.
        assert _digest_verifies(rows[CONTROL_FEATURE]), (
            "the control row (already fingerprinted, never redacted) must "
            "keep verifying under the production HMAC scheme"
        )
        # And no raw token material may remain anywhere in user_id.
        user_ids = [row[2] for row in rows.values()]
        assert CURRENT_TOKEN not in user_ids and ROTATED_TOKEN not in user_ids
