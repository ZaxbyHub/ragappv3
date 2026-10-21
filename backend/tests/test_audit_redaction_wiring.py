"""Additive wiring and behavior pins for the #894 audit-row redaction.

The frozen checks (test_audit_hmac_redaction.py C8/C9) accept any
implementation whose rows either verify or self-identify — including a
tombstone-everything one. These additive tests pin the exact contract the
plan (05-fix-plan.md Revision 3, plan-critic rounds 1-3) specifies: exact
identity values per branch (scoped fingerprint for the current token,
REDACTED_LEGACY_IDENTITY for rotated/unknown), verifying digests for BOTH
branches computed under the production message layout with the PASSED key,
NULL-ip handling, control rows untouched, journal semantics, and the
lifespan wiring helper's success and fail-soft behavior.
"""

import ast
import hashlib
import hmac
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.api.routes.admin import _compute_hmac
from app.config import settings
from app.models.database import run_migrations
from app.security import admin_scope_fingerprint
from app.services import audit_redaction
from app.services.audit_redaction import (
    REDACTED_LEGACY_IDENTITY,
    redact_historical_audit_rows,
    run_startup_audit_redaction,
)

CURRENT_TOKEN = "wiring-current-admin-token-0001"
ROTATED_TOKEN = "wiring-rotated-admin-token-77c1"
HEX_FP = "0123456789abcdef"
TEST_KEY = b"wiring-test-key"
TEST_KEY_VERSION = "v-wiring"
SEED_IP = "10.1.2.3"


@contextmanager
def _temp_db():
    temp_dir = tempfile.mkdtemp(prefix="audit_redaction_wiring_")
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
    with patch.object(settings, "admin_secret_token", CURRENT_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {CURRENT_TOKEN: ["admin:config"]}
        ):
            yield


def _insert(conn, feature, enabled, user_id, ip=SEED_IP):
    digest, ts = _compute_hmac(TEST_KEY, feature, enabled, user_id, ip)
    conn.execute(
        "INSERT INTO audit_toggle_log"
        "(feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (feature, int(enabled), user_id, ip, ts, "v-seed", digest),
    )


def _seed(db_path, *, include_null_ip=True):
    conn = sqlite3.connect(db_path)
    try:
        _insert(conn, "w_cur", True, CURRENT_TOKEN)
        _insert(conn, "w_rot", False, ROTATED_TOKEN)
        if include_null_ip:
            _insert(conn, "w_cur_null_ip", True, CURRENT_TOKEN, ip=None)
        _insert(conn, "w_fp_control", True, HEX_FP)
        _insert(conn, "w_null", True, None)
        _insert(conn, "w_empty", True, "")
        _insert(conn, "w_tomb_control", True, REDACTED_LEGACY_IDENTITY)
        conn.commit()
    finally:
        conn.close()


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]: tuple(r)
            for r in conn.execute(
                "SELECT feature, enabled, user_id, ip, timestamp, key_version, "
                "hmac_sha256 FROM audit_toggle_log ORDER BY id"
            )
        }
    finally:
        conn.close()


def _verifies(row, key=TEST_KEY) -> bool:
    feature, enabled, user_id, ip, timestamp, _kv, digest = row
    message = f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"
    expected = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def _journal_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT migration_name, phase, outcome FROM migration_journal "
            "WHERE migration_name = 'redact_historical_audit_rows'"
        ).fetchall()
    finally:
        conn.close()


def test_redaction_exact_values_and_verifying_digests():
    """Both branches carry exact identities AND verifying digests.

    The marker alone must not satisfy these pins: the current-token row's
    user_id must equal the scoped fingerprint computed under the same
    patched settings, the rotated row's must equal REDACTED_LEGACY_IDENTITY
    exactly, and BOTH digests must verify under the production message
    layout recomputed with the key passed to the routine.
    """
    with _temp_db() as db_path:
        _seed(db_path)
        with _configured_admin():
            expected_fp = admin_scope_fingerprint(CURRENT_TOKEN)
            count = redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        rows = _rows(db_path)

        assert count == 3, f"expected 3 redactions (current, rotated, null-ip), got {count}"
        assert rows["w_cur"][2] == expected_fp
        assert rows["w_rot"][2] == REDACTED_LEGACY_IDENTITY
        assert rows["w_cur_null_ip"][2] == expected_fp
        # BOTH rewritten branches verify (not merely marked).
        assert _verifies(rows["w_cur"])
        assert _verifies(rows["w_rot"])
        assert _verifies(rows["w_cur_null_ip"])
        # key_version updated truthfully to the passed value.
        assert rows["w_cur"][5] == TEST_KEY_VERSION
        assert rows["w_rot"][5] == TEST_KEY_VERSION
        # Timestamps preserved verbatim: the digest verification above is the
        # load-bearing pin (it recomputes over the row's STORED timestamp, so
        # a timestamp-regeneration mutation breaks it — proven by the
        # reviewer's hand-probe 3); asserting the timestamp against itself
        # would be a tautology, so no separate line here.


def test_untouched_classes_byte_identical_and_journal_silent_on_clean():
    """Control classes pass through unchanged; clean boots journal nothing."""
    with _temp_db() as db_path:
        _seed(db_path)
        before = _rows(db_path)
        with _configured_admin():
            redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        after = _rows(db_path)
        for feature in ("w_fp_control", "w_null", "w_empty", "w_tomb_control"):
            assert after[feature] == before[feature], f"{feature} was modified"
        # Idempotent second run: nothing left to do, nothing changes.
        with _configured_admin():
            second = redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        assert second == 0
        assert _rows(db_path) == after
        # Journal: exactly start + succeeded (no failed), no extra rows.
        assert _journal_rows(db_path) == [
            ("redact_historical_audit_rows", "start", "ok"),
            ("redact_historical_audit_rows", "succeeded", "ok"),
        ]
        # A clean database journals nothing at all.
        with _temp_db() as clean_path:
            with _configured_admin():
                assert redact_historical_audit_rows(clean_path, TEST_KEY, TEST_KEY_VERSION) == 0
            assert _journal_rows(clean_path) == []


def test_null_ip_row_verifies_via_or_empty_fallback():
    """A real-shape NULL-ip row must still verify after redaction."""
    with _temp_db() as db_path:
        _seed(db_path)
        with _configured_admin():
            redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        row = _rows(db_path)["w_cur_null_ip"]
        assert row[3] is None  # ip untouched
        assert _verifies(row), "NULL-ip row digest must verify via the or '' fallback"


def test_wiring_helper_success_and_flag():
    """run_startup_audit_redaction drives the routine exactly as lifespan does."""
    with _temp_db() as db_path:
        _seed(db_path)
        # settings.sqlite_path is a read-only property on the real Settings
        # object, so swap the module-level settings reference inside
        # audit_redaction for a stub carrying the temp path (the routine also
        # reads admin_secret_token from it, so the stub carries the patched
        # admin values too — exactly what the lifespan context provides).
        stub_settings = SimpleNamespace(
            sqlite_path=db_path,
            admin_secret_token=CURRENT_TOKEN,
            admin_token_scopes={CURRENT_TOKEN: ["admin:config"]},
        )
        with patch.object(audit_redaction, "settings", stub_settings):
            fake_sm = SimpleNamespace(
                get_hmac_key=lambda: (TEST_KEY, TEST_KEY_VERSION)
            )
            app = SimpleNamespace(state=SimpleNamespace(secret_manager=fake_sm))
            run_startup_audit_redaction(app)
        rows = _rows(db_path)
        assert rows["w_cur"][2] == admin_scope_fingerprint(CURRENT_TOKEN)
        assert rows["w_rot"][2] == REDACTED_LEGACY_IDENTITY
        assert app.state.audit_redaction_ok is True


def test_wiring_helper_fail_soft_flag_and_no_escape():
    """A failing redaction must not propagate; it flags False instead."""
    with _configured_admin():
        with patch.object(
            audit_redaction,
            "redact_historical_audit_rows",
            side_effect=sqlite3.OperationalError("disk locked"),
        ):
            fake_sm = SimpleNamespace(
                get_hmac_key=lambda: (TEST_KEY, TEST_KEY_VERSION)
            )
            app = SimpleNamespace(state=SimpleNamespace(secret_manager=fake_sm))
            # Must not raise: fail-soft contract.
            run_startup_audit_redaction(app)
    assert app.state.audit_redaction_ok is False


def test_lifespan_wiring_source_pin():
    """Belt-only AST pin: lifespan calls the helper after the secret manager.

    Asserts both position (after ``app.state.secret_manager = SecretManager()``)
    and argument shape (``asyncio.to_thread(run_startup_audit_redaction, app)``).
    The behavior tests above are the primary guardrail; this pin catches a
    silent unwiring that they cannot see (a call removed from lifespan while
    the helper still passes its own tests).
    """
    source = Path("app/lifespan.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigns = {}
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "secret_manager"
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "state"
                ):
                    assigns[id(node)] = node.lineno
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "to_thread"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "run_startup_audit_redaction"
            ):
                calls.append(node.lineno)
    assert calls, "lifespan no longer calls run_startup_audit_redaction via to_thread"
    assert assigns, "lifespan no longer assigns app.state.secret_manager"
    assert min(calls) > max(
        assigns.values()
    ), "run_startup_audit_redaction must be called after the secret manager exists"


@pytest.mark.parametrize(
    "mutation", ["identity", "digest", "predicate_glob", "key_version"]
)
def test_frozen_contract_bites_under_mutation(mutation):
    """Named-mutation discipline: each load-bearing implementation choice has
    a test above that fails when it is mutated (documented mapping, asserted
    here by construction — the mutations are probed at Phase 4.5 in a
    throwaway worktree; this test documents the mapping and guards the
    module constants the probes rely on)."""
    if mutation == "identity":
        assert REDACTED_LEGACY_IDENTITY == "redacted:legacy-admin-token"
        assert "redact" in REDACTED_LEGACY_IDENTITY.lower()
    elif mutation == "digest":
        # The message builder must reproduce the production layout exactly.
        assert audit_redaction._audit_message("f", True, "u", "ip", "t") == "f|1|u|ip|t"
        assert audit_redaction._audit_message("f", False, None, None, "t") == "f|0|||t"
    elif mutation == "predicate_glob":
        # "[0-9a-f]" * 16 is database.py:7536's shape: ONE bracket class
        # repeated 16 times — a glob matching exactly 16 hex chars (the
        # string itself is longer; the GLOB width is 16).
        assert audit_redaction._FINGERPRINT_GLOB == "[0-9a-f]" * 16
        conn = sqlite3.connect(":memory:")
        try:
            hex16 = conn.execute(
                "SELECT '0123456789abcdef' GLOB ?", (audit_redaction._FINGERPRINT_GLOB,)
            ).fetchone()[0]
            not_hex = conn.execute(
                "SELECT 'zzzzzzzzzzzzzzzz' GLOB ?", (audit_redaction._FINGERPRINT_GLOB,)
            ).fetchone()[0]
            short = conn.execute(
                "SELECT '0123456789abcde' GLOB ?", (audit_redaction._FINGERPRINT_GLOB,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert (hex16, not_hex, short) == (1, 0, 0), (
            "fingerprint glob must match exactly-16 lowercase hex and nothing else"
        )
    else:
        # key_version propagation is pinned behaviorally in
        # test_redaction_exact_values_and_verifying_digests (asserts the
        # stored key_version equals TEST_KEY_VERSION after redaction) and is
        # killed by mutation probe M4; a constant assertion here would be a
        # truthy tautology (reviewer round-1 nit), so this arm is doc-only.
        pass
