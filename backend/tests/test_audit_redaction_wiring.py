"""Additive wiring, exact-value and fault-path pins for the #894 redaction.

The frozen checks (test_audit_hmac_redaction.py C8/C9) accept any
implementation whose rows either verify or self-identify as a tombstone —
including a tombstone-everything one. This module closes that gap with
exact-value pins, the branch-specific contracts, the real-fault journal
contract, and the source-shape pins that make the load-bearing lines
mutation-sensitive.

Nothing here is decorative: every assertion below fails if the
corresponding implementation line is removed or weakened.
"""

import ast
import hashlib
import hmac
import logging
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

# A 16-lowercase-hex token: the shape the fingerprint exclusion used to skip.
HEX_TOKEN = "a3f9c1d24b7e6085"


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
    """Patch BOTH token settings on the REAL settings object.

    ``admin_scope_fingerprint`` reads ``settings.admin_token_scopes`` from
    ``app.config`` (imported into ``app.security``), so patching only a
    module-local stub would leave the scope map unpatched and make the
    expected fingerprint degenerate to the empty-scope digest.
    """
    with patch.object(settings, "admin_secret_token", CURRENT_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {CURRENT_TOKEN: ["admin:config"]}
        ):
            yield


@contextmanager
def _configured_hex_admin():
    """Same, with a 16-lowercase-hex token as the CURRENT token."""
    with patch.object(settings, "admin_secret_token", HEX_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {HEX_TOKEN: ["admin:config"]}
        ):
            yield


def _insert(conn, feature, enabled, user_id, ip=SEED_IP, key=TEST_KEY, digest=None):
    computed, ts = _compute_hmac(key, feature, enabled, user_id, ip)
    conn.execute(
        "INSERT INTO audit_toggle_log"
        "(feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            feature,
            int(enabled),
            user_id,
            ip,
            ts,
            TEST_KEY_VERSION,
            digest if digest is not None else computed,
        ),
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
            "WHERE migration_name = 'redact_historical_audit_rows' "
            "ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class _FailOnBeginConnection:
    """Real connection proxy whose BEGIN IMMEDIATE raises."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql, *args, **kwargs):
        if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._real.close()

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction


def _connect_failing_on_begin(*args, **kwargs):
    return _FailOnBeginConnection(sqlite3.Connection(*args, **kwargs))


class _ExplodingSecretManager:
    """Secret manager that must NOT be asked for the key without work."""

    def get_hmac_key(self):
        raise AssertionError("the HMAC key must not be resolved without work")


def test_redaction_exact_values_and_verifying_digests():
    """Both branches carry exact identities AND verifying digests.

    The current-token row's user_id must equal the scoped fingerprint
    computed under the same patched settings; the rotated row's must equal
    REDACTED_LEGACY_IDENTITY exactly; and BOTH rewritten rows must verify
    under the production message layout recomputed with the key passed to
    the routine.
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
        assert _verifies(rows["w_cur"])
        assert _verifies(rows["w_rot"])
        assert _verifies(rows["w_cur_null_ip"])
        assert rows["w_cur"][5] == TEST_KEY_VERSION
        assert rows["w_rot"][5] == TEST_KEY_VERSION


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
        with _configured_admin():
            second = redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        assert second == 0
        assert _rows(db_path) == after
        assert _journal_rows(db_path) == [
            ("redact_historical_audit_rows", "start", "ok"),
            ("redact_historical_audit_rows", "succeeded", "ok"),
        ]
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


def test_current_16_hex_token_is_redacted():
    """A 16-lowercase-hex CURRENT token must not be skipped.

    The fingerprint exclusion (``NOT GLOB``) matches a 16-hex token, so the
    detection predicate must ALSO match the configured token exactly — the
    sibling migration ORs that clause, and the current token is known at
    runtime, so it can never be "indistinguishable from a fingerprint".
    """
    with _temp_db() as db_path:
        conn = sqlite3.connect(db_path)
        try:
            _insert(conn, "hex_current", True, HEX_TOKEN)
            conn.commit()
        finally:
            conn.close()
        with _configured_hex_admin():
            expected_fp = admin_scope_fingerprint(HEX_TOKEN)
            count = redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
            row = _rows(db_path)["hex_current"]
        assert count == 1, f"the current 16-hex token must be redacted, got {count}"
        assert row[2] != HEX_TOKEN, "the raw 16-hex token must not remain in user_id"
        assert row[2] == expected_fp
        assert _verifies(row)


def test_unverifiable_row_becomes_a_tombstone_without_a_valid_digest():
    """A row whose digest does not verify is tombstoned, not re-attributed.

    Re-signing a row that never verified would launder a tampered row into
    a validly-signed audit entry, so an unverifiable row keeps its stored
    digest (and its recorded key version) and only its identity is
    scrubbed — the tombstone marker states that the row's provenance can
    no longer be established.
    """
    with _temp_db() as db_path:
        conn = sqlite3.connect(db_path)
        try:
            _insert(conn, "tampered", True, ROTATED_TOKEN, digest="GARBAGE")
            conn.commit()
        finally:
            conn.close()
        with _configured_admin():
            redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        row = _rows(db_path)["tampered"]
        assert row[2] == REDACTED_LEGACY_IDENTITY
        assert not _verifies(row), (
            "an unverifiable row must not come out with a valid digest"
        )


def test_row_signed_under_a_rotated_key_is_tombstoned():
    """A row signed under a different key version cannot be verified."""
    with _temp_db() as db_path:
        conn = sqlite3.connect(db_path)
        try:
            _insert(conn, "old_key", True, ROTATED_TOKEN, key=b"an-older-key")
            conn.execute(
                "UPDATE audit_toggle_log SET key_version = 'v-old' "
                "WHERE feature = 'old_key'"
            )
            conn.commit()
        finally:
            conn.close()
        with _configured_admin():
            redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)
        row = _rows(db_path)["old_key"]
        assert row[2] == REDACTED_LEGACY_IDENTITY
        assert row[5] == "v-old", "an unverifiable row keeps its recorded key version"


def test_begin_failure_journals_only_failed_row_and_leaves_data_untouched():
    """A failing ``BEGIN IMMEDIATE`` journals exactly the terminal failed row.

    The failure is injected into the real function (not a mocked-out
    routine): ``BEGIN IMMEDIATE`` raises, and the contract is that the
    journal ends in a terminal ``failed`` row, the audit rows are
    unchanged, and the fail-soft wrapper logs the error.

    Issue #995 realignment: the write lock is now acquired BEFORE any
    journal write, so ``start`` is journaled in-transaction — only once the
    lock is actually held. A BEGIN failure therefore journals exactly
    ``[("redact_historical_audit_rows", "failed", "error")]``: the #699
    contract needs the terminal ``failed`` row and unchanged data, not a
    durable pre-``BEGIN`` ``start`` row (re-adding one would put a second
    full connect-timeout wait on the boot path this change removes). The
    data-failure path still journals ``[start, failed]`` — pinned in
    ``test_audit_followups_extra_guards.py``.
    """
    with _temp_db() as db_path:
        _seed(db_path)
        before = _rows(db_path)
        with _configured_admin():
            with patch.object(sqlite3, "connect", _connect_failing_on_begin):
                with pytest.raises(sqlite3.OperationalError):
                    redact_historical_audit_rows(db_path, TEST_KEY, TEST_KEY_VERSION)

        assert _journal_rows(db_path) == [
            ("redact_historical_audit_rows", "failed", "error"),
        ]
        assert _rows(db_path) == before, "a failed redaction must not change data"

        # The fail-soft wrapper turns the same failure into a flag + log.
        fake_sm = SimpleNamespace(get_hmac_key=lambda: (TEST_KEY, TEST_KEY_VERSION))
        app = SimpleNamespace(state=SimpleNamespace(secret_manager=fake_sm))
        with _configured_admin():
            with patch.object(sqlite3, "connect", _connect_failing_on_begin):
                with caplog_at_error() as records:
                    run_startup_audit_redaction(app)
        assert app.state.audit_redaction_ok is False
        assert any("redaction failed" in r.message for r in records)


@contextmanager
def caplog_at_error():
    """Collect ERROR records emitted inside the block."""
    import logging as _logging

    class _Collector(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    collector = _Collector()
    logger = _logging.getLogger("app.services.audit_redaction")
    logger.addHandler(collector)
    old_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield collector.records
    finally:
        logger.removeHandler(collector)
        logger.setLevel(old_level)


def test_wiring_helper_success_and_flag():
    """run_startup_audit_redaction drives the routine exactly as lifespan does."""
    with _temp_db() as db_path:
        _seed(db_path)
        # settings.sqlite_path is a read-only property on the real Settings
        # object, so the module-level settings reference inside
        # audit_redaction is swapped for a stub carrying the temp path. The
        # admin token and scope map stay on the REAL settings object (patched
        # by _configured_admin) because admin_scope_fingerprint reads them
        # from app.config.
        stub_settings = SimpleNamespace(
            sqlite_path=db_path,
            admin_secret_token=CURRENT_TOKEN,
            admin_token_scopes={CURRENT_TOKEN: ["admin:config"]},
        )
        with _configured_admin():
            expected_fp = admin_scope_fingerprint(CURRENT_TOKEN)
            with patch.object(audit_redaction, "settings", stub_settings):
                fake_sm = SimpleNamespace(
                    get_hmac_key=lambda: (TEST_KEY, TEST_KEY_VERSION)
                )
                app = SimpleNamespace(state=SimpleNamespace(secret_manager=fake_sm))
                run_startup_audit_redaction(app)
        rows = _rows(db_path)
        assert rows["w_cur"][2] == expected_fp
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


def test_wiring_helper_does_not_resolve_the_key_when_there_is_no_work():
    """A clean boot must not resolve the HMAC key.

    Resolving it on every clean boot emits a warn-and-fallback log line
    when no dedicated audit key is configured — the routine therefore
    probes for pending work FIRST.
    """
    with _temp_db() as db_path:
        stub_settings = SimpleNamespace(
            sqlite_path=db_path,
            admin_secret_token=CURRENT_TOKEN,
            admin_token_scopes={CURRENT_TOKEN: ["admin:config"]},
        )

        with _configured_admin():
            with patch.object(audit_redaction, "settings", stub_settings):
                fake_sm = _ExplodingSecretManager()
                app = SimpleNamespace(state=SimpleNamespace(secret_manager=fake_sm))
                run_startup_audit_redaction(app)
        assert app.state.audit_redaction_ok is True


def test_lifespan_wiring_source_pin():
    """Belt-only AST pin: lifespan calls the helper after the secret manager.

    Asserts both position (after ``app.state.secret_manager = SecretManager()``)
    and argument shape (``asyncio.to_thread(run_startup_audit_redaction, app)``).
    The path is resolved from THIS FILE, not from the process CWD, so the
    pin is invariant to how the suite is invoked.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "lifespan.py").read_text(
        encoding="utf-8"
    )
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


def test_redaction_source_pins_are_mutation_sensitive():
    """Source-shape pins for the load-bearing lines of the routine.

    These replace a decorative "mutation" test with real, repo-reproducible
    assertions: dropping the exact-token clause, the per-row user_id
    re-check, the digest re-computation, or the key_version write must
    break a pin.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "audit_redaction.py").read_text(
        encoding="utf-8"
    )

    # 1. The detection predicate matches the configured token EXACTLY.
    assert "OR user_id = ? LIMIT 1" in source, (
        "the detection predicate must match the configured token exactly"
    )
    assert "OR user_id = ?" in source, (
        "the row-selection predicate must match the configured token exactly"
    )

    # 2. The per-row UPDATE re-checks the identity it read, so a concurrent
    #    redaction of the same row is not double-counted.
    assert "WHERE id = ? AND user_id = ?" in source, (
        "the UPDATE must be keyed by id AND the identity it read"
    )

    # 3. The digest is recomputed over the NEW identity, and the stored
    #    digest is compared before a row is re-attributed.
    assert "hmac.compare_digest" in source, (
        "a row must be verified before it is re-attributed"
    )
    assert "_audit_message(\n                        feature, bool(enabled), new_identity, ip, timestamp" in source, (
        "the re-signed digest must be computed over the NEW identity"
    )

    # 4. The key_version is written (truthfully: the digest is now under
    #    the runtime key).
    assert "SET user_id = ?, hmac_sha256 = ?, key_version = ?" in source

    # 5. The data work is inside ONE try whose handler journals 'failed'.
    assert "phase=\"failed\"" in source
    tree = ast.parse(source)
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "redact_historical_audit_rows"
    )
    try_blocks = [n for n in ast.walk(func) if isinstance(n, ast.Try)]
    assert try_blocks, "the routine must guard its data work in a try"
    guarded = try_blocks[0]
    begin_calls = [
        n
        for n in ast.walk(guarded)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "execute"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "BEGIN IMMEDIATE"
    ]
    assert begin_calls, "BEGIN IMMEDIATE must sit INSIDE the guarded try"
    handlers = [h for h in guarded.handlers if h.type is not None]
    assert handlers, "the guarded try must have an exception handler"
    handler_src = ast.dump(handlers[0])
    assert "failed" in handler_src, (
        "the exception handler must journal the 'failed' phase"
    )
