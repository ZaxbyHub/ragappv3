"""Issue #561 regression: document_actions audit rows on the shipped configuration.

Unmocked end-to-end coverage for the document-action audit path. The secret
manager is the REAL SecretManager on both wiring paths — the request-scoped
dependency (upload/retry) and the app-state attribute the delete route reads
(mirroring lifespan wiring) — with no stubbed key material anywhere. Three
existing suites (test_documents_delete_audit, test_documents_auth,
test_document_actions_audit) inject a fake key and can never see the
configuration failure this file pins: with no AUDIT_HMAC_KEY* environment
variable set — exactly what .env.example/docker-compose ship — every audit
row must still be written, keyed by the shared fallback derivation from
JWT_SECRET_KEY/ADMIN_SECRET_TOKEN (identical to security_audit_log's key).

The delete route resolves its manager from request.app.state rather than the
dependency, so the harness wires the same real instance through setattr()
below; no mock ever touches that attribute.
"""

import hashlib
import hmac
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.api.deps import (
    get_background_processor,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_secret_manager,
    get_vector_store,
)
from app.config import settings
from app.main import app
from app.services.auth_service import compute_client_fingerprint, create_access_token
from app.services.secret_manager import SecretManager

_AUDIT_KEY_VARS = ("AUDIT_HMAC_KEY", "AUDIT_HMAC_KEY_V1", "AUDIT_HMAC_KEY_V2")
# A dedicated audit key distinct from every secret conftest sets, so the
# explicit-key-wins case can prove the digest does NOT verify with the JWT key.
_DEDICATED_AUDIT_KEY = "issue561-dedicated-audit-key-0123456789abcdef-0123456789abcdef"


class ShippedConfigAuditBase(unittest.TestCase):
    """Boots the app the way the shipped configuration does: no audit key."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp(prefix="issue561_shipped_")
        self._orig_users = settings.users_enabled
        self._orig_data_dir = settings.data_dir
        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_state_sm = getattr(app.state, "secret_manager", None)
        self._saved_audit_env = {v: os.environ.get(v) for v in _AUDIT_KEY_VARS}
        for var in _AUDIT_KEY_VARS:
            os.environ.pop(var, None)
        # Reset the warn-once module state so every test observes the same
        # first-call behavior regardless of execution order (PRR-011).
        import app.services.secret_manager as _sm_module

        self._saved_warn_state = (
            set(_sm_module._fallback_warned_versions),
            set(_sm_module._weak_key_warned_versions),
        )
        _sm_module._fallback_warned_versions.clear()
        _sm_module._weak_key_warned_versions.clear()
        settings.data_dir = Path(self._temp_dir)
        settings.users_enabled = True

        from app.models.database import (
            SQLiteConnectionPool,
            _pool_cache,
            _pool_cache_lock,
            init_db,
            run_migrations,
        )

        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        db_path = str(Path(self._temp_dir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)
        # The upload path requires the real pool (its `.connection` context
        # manager), so use SQLiteConnectionPool rather than the tests helper.
        self._pool = SQLiteConnectionPool(db_path)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        mock_vs = MagicMock()
        mock_vs.db = None
        mock_vs.delete_by_file = AsyncMock(return_value=1)
        mock_emb = MagicMock()
        mock_bp = MagicMock()
        mock_bp.is_running = True
        mock_bp.enqueue = AsyncMock(return_value=True)

        # Real, unmocked SecretManager on BOTH wiring paths: the dependency
        # (upload/retry) and the app-state attribute (delete reads it
        # directly). setattr keeps the harness explicit about the second path.
        self._real_sm = SecretManager()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: mock_vs
        app.dependency_overrides[get_embedding_service] = lambda: mock_emb
        app.dependency_overrides[get_db_pool] = lambda: self._pool
        app.dependency_overrides[get_background_processor] = lambda: mock_bp
        app.dependency_overrides[get_secret_manager] = lambda: self._real_sm
        setattr(app.state, "secret_manager", self._real_sm)

        conn = self._pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active)"
                " VALUES (1, 'superadmin', ?, 'Super Admin', 'superadmin', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active)"
                " VALUES (3, 'member1', ?, 'Member One', 'member', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2, 'Shipped Vault', 'shipped')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by)"
                " VALUES (2, 3, 'admin', 1)"
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)

        # 500s must surface as HTTP responses (what a deployment observes),
        # never as in-process exceptions.
        self.client = TestClient(app, raise_server_exceptions=False)
        # Cleanup order (unittest runs cleanups LIFO): env/warn-state restore
        # is registered LAST so it runs FIRST, before the app teardown — any
        # teardown code that touched secret state would then see the original
        # environment, not the scrubbed one (PRR-015).
        self.addCleanup(self._teardown_app)
        self.addCleanup(self._restore_warn_state)
        self.addCleanup(self._restore_settings_and_env)

    def _restore_warn_state(self):
        import app.services.secret_manager as _sm_module

        (
            fallback_versions,
            weak_key_versions,
        ) = self._saved_warn_state
        _sm_module._fallback_warned_versions.clear()
        _sm_module._fallback_warned_versions.update(fallback_versions)
        _sm_module._weak_key_warned_versions.clear()
        _sm_module._weak_key_warned_versions.update(weak_key_versions)

    def _restore_settings_and_env(self):
        settings.jwt_secret_key = self._orig_jwt_secret
        for var, value in self._saved_audit_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def _teardown_app(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()
        settings.users_enabled = self._orig_users
        settings.data_dir = self._orig_data_dir
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_embedding_service, None)
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_background_processor, None)
        app.dependency_overrides.pop(get_secret_manager, None)
        setattr(app.state, "secret_manager", self._orig_state_sm)
        self._pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _headers(self, user_id, username, role):
        ua = f"issue561-{username}-agent"
        token = create_access_token(
            user_id, username, role, client_fingerprint=compute_client_fingerprint(ua)
        )
        return {"Authorization": f"Bearer {token}", "user-agent": ua}

    def _member_headers(self):
        return self._headers(3, "member1", "member")

    def _superadmin_headers(self):
        return self._headers(1, "superadmin", "superadmin")

    def _seed_file(self, name):
        conn = self._pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status, parsed_text)"
                " VALUES (?,?,?,?,?,?)",
                (2, f"/uploads/{name}", name, 13, "indexed", "issue 561 content"),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            self._pool.release_connection(conn)

    def _audit_rows(self, file_id, action):
        conn = self._pool.get_connection()
        try:
            return conn.execute(
                "SELECT file_id, action, status, user_id, hmac_sha256 FROM document_actions"
                " WHERE file_id = ? AND action = ?",
                (file_id, action),
            ).fetchall()
        finally:
            self._pool.release_connection(conn)

    def _expected_digest(self, key_bytes, row):
        message = f"{row['file_id']}|{row['action']}|{row['status']}|{row['user_id']}"
        return hmac.new(key_bytes, message.encode("utf-8"), hashlib.sha256).hexdigest()


class TestUploadAuditRow(ShippedConfigAuditBase):
    def test_upload_writes_one_verifiable_row_with_fallback_key(self):
        resp = self.client.post(
            "/api/documents/upload?vault_id=2",
            headers=self._member_headers(),
            files={"file": ("shipped.txt", b"issue 561 shipped-config content", "text/plain")},
        )
        assert resp.status_code in (200, 201), (
            f"upload itself failed: {resp.status_code} {resp.text[:300]}"
        )
        file_id = resp.json()["file_id"]
        rows = self._audit_rows(file_id, "upload")
        assert len(rows) == 1, (
            f"expected exactly 1 upload document_actions row for file_id={file_id},"
            f" found {len(rows)} — audit write was swallowed on the shipped configuration"
        )
        fallback_key = settings.jwt_secret_key.strip().encode("utf-8")
        assert rows[0]["hmac_sha256"] == self._expected_digest(fallback_key, rows[0]), (
            "upload digest does not verify with the fallback-derived (JWT) key"
        )


class TestDeleteAuditRow(ShippedConfigAuditBase):
    def test_delete_writes_one_verifiable_row_with_fallback_key(self):
        file_id = self._seed_file("delete-me.txt")
        resp = self.client.delete(f"/api/documents/{file_id}", headers=self._member_headers())
        assert resp.status_code == 200, f"delete itself failed: {resp.status_code} {resp.text[:300]}"
        rows = self._audit_rows(file_id, "delete")
        assert len(rows) == 1, (
            f"expected exactly 1 delete document_actions row, found {len(rows)}"
            " — audit write was swallowed on the shipped configuration"
        )
        fallback_key = settings.jwt_secret_key.strip().encode("utf-8")
        assert rows[0]["hmac_sha256"] == self._expected_digest(fallback_key, rows[0]), (
            "delete digest does not verify with the fallback-derived (JWT) key"
        )


class TestRetryAuditRow(ShippedConfigAuditBase):
    def test_retry_returns_2xx_and_writes_one_scheduled_row(self):
        file_id = self._seed_file("retry-me.txt")
        resp = self.client.post(
            f"/api/documents/admin/retry/{file_id}", headers=self._superadmin_headers()
        )
        assert resp.status_code in (200, 201, 204), (
            f"retry returned {resp.status_code} (expected 2xx; at the pre-fix base this"
            " endpoint 500-ed on the shipped configuration): {resp.text[:300]}"
        )
        rows = self._audit_rows(file_id, "retry")
        assert len(rows) == 1, (
            f"expected exactly 1 retry document_actions row, found {len(rows)}"
        )
        assert rows[0]["status"] == "scheduled", (
            f"retry audit row recorded status {rows[0]['status']!r}, expected"
            " 'scheduled' — a wrong-status row must fail this test"
        )
        fallback_key = settings.jwt_secret_key.strip().encode("utf-8")
        assert rows[0]["hmac_sha256"] == self._expected_digest(fallback_key, rows[0]), (
            "retry digest does not verify with the fallback-derived (JWT) key"
        )

    def test_retry_already_in_progress_records_that_status(self):
        file_id = self._seed_file("retry-busy.txt")
        # Override ONLY the enqueue result for this test: the endpoint must
        # record status 'already_in_progress' (not 'scheduled') when the
        # background processor reports the file as already enqueued.
        busy_bp = MagicMock()
        busy_bp.is_running = True
        busy_bp.enqueue = AsyncMock(return_value=False)
        app.dependency_overrides[get_background_processor] = lambda: busy_bp
        self.addCleanup(app.dependency_overrides.pop, get_background_processor, None)
        resp = self.client.post(
            f"/api/documents/admin/retry/{file_id}", headers=self._superadmin_headers()
        )
        assert resp.status_code in (200, 201, 204), (
            f"retry returned {resp.status_code}: {resp.text[:300]}"
        )
        rows = self._audit_rows(file_id, "retry")
        assert len(rows) == 1, f"expected exactly 1 retry row, found {len(rows)}"
        assert rows[0]["status"] == "already_in_progress", (
            f"expected 'already_in_progress' audit status, got {rows[0]['status']!r}"
        )
        fallback_key = settings.jwt_secret_key.strip().encode("utf-8")
        assert rows[0]["hmac_sha256"] == self._expected_digest(fallback_key, rows[0]), (
            "already_in_progress digest does not verify with the fallback key"
        )


class TestBatchDeleteAuditRow(ShippedConfigAuditBase):
    def test_batch_delete_writes_one_row_per_file(self):
        first = self._seed_file("batch-1.txt")
        second = self._seed_file("batch-2.txt")
        resp = self.client.post(
            "/api/documents/batch",
            headers=self._member_headers(),
            json={"file_ids": [str(first), str(second)]},
        )
        assert resp.status_code == 200, (
            f"batch delete itself failed: {resp.status_code} {resp.text[:300]}"
        )
        payload = resp.json()
        assert payload.get("deleted_count") == 2, (
            f"expected 2 deletions, got: {payload}"
        )
        for file_id in (first, second):
            rows = self._audit_rows(file_id, "delete")
            assert len(rows) == 1, (
                f"expected exactly 1 delete row for file_id={file_id}, found {len(rows)}"
            )
            fallback_key = settings.jwt_secret_key.strip().encode("utf-8")
            assert rows[0]["hmac_sha256"] == self._expected_digest(fallback_key, rows[0]), (
                f"batch delete digest for file_id={file_id} does not verify with"
                " the fallback-derived (JWT) key"
            )


class TestExplicitAuditKeyWins(ShippedConfigAuditBase):
    def test_dedicated_env_key_is_used_and_fallback_is_not(self):
        assert _DEDICATED_AUDIT_KEY != settings.jwt_secret_key.strip()
        assert len(_DEDICATED_AUDIT_KEY) >= 32
        os.environ["AUDIT_HMAC_KEY_V1"] = _DEDICATED_AUDIT_KEY
        try:
            resp = self.client.post(
                "/api/documents/upload?vault_id=2",
                headers=self._member_headers(),
                files={"file": (
                    "dedicated-key.txt", b"issue 561 dedicated audit key content", "text/plain"
                )},
            )
            assert resp.status_code in (200, 201), (
                f"upload itself failed: {resp.status_code} {resp.text[:300]}"
            )
            file_id = resp.json()["file_id"]
            rows = self._audit_rows(file_id, "upload")
            assert len(rows) == 1, f"expected exactly 1 upload row, found {len(rows)}"
            dedicated = _DEDICATED_AUDIT_KEY.encode("utf-8")
            jwt_key = settings.jwt_secret_key.strip().encode("utf-8")
            assert rows[0]["hmac_sha256"] == self._expected_digest(dedicated, rows[0]), (
                "digest does not verify with the dedicated AUDIT_HMAC_KEY_V1 key"
            )
            assert rows[0]["hmac_sha256"] != self._expected_digest(jwt_key, rows[0]), (
                "digest verified with the JWT fallback key — the dedicated env key"
                " must take precedence"
            )
        finally:
            os.environ.pop("AUDIT_HMAC_KEY_V1", None)

    def test_versioned_key_wins_over_bare_key_when_both_set(self):
        # Pins the documented precedence AUDIT_HMAC_KEY_<VERSION> ->
        # AUDIT_HMAC_KEY (secret_manager.get_hmac_key): with BOTH set to
        # distinct keys, the versioned key must key the digest.
        bare_key = "issue561-bare-audit-key-0123456789abcdef-0123456789abcdef"
        assert bare_key != _DEDICATED_AUDIT_KEY
        os.environ["AUDIT_HMAC_KEY"] = bare_key
        os.environ["AUDIT_HMAC_KEY_V1"] = _DEDICATED_AUDIT_KEY
        try:
            resp = self.client.post(
                "/api/documents/upload?vault_id=2",
                headers=self._member_headers(),
                files={"file": (
                    "precedence.txt", b"issue 561 precedence content", "text/plain"
                )},
            )
            assert resp.status_code in (200, 201), (
                f"upload itself failed: {resp.status_code} {resp.text[:300]}"
            )
            file_id = resp.json()["file_id"]
            rows = self._audit_rows(file_id, "upload")
            assert len(rows) == 1, f"expected exactly 1 upload row, found {len(rows)}"
            versioned = _DEDICATED_AUDIT_KEY.encode("utf-8")
            bare = bare_key.encode("utf-8")
            assert rows[0]["hmac_sha256"] == self._expected_digest(versioned, rows[0]), (
                "digest does not verify with the versioned AUDIT_HMAC_KEY_V1 key"
                " — versioned key must win over bare AUDIT_HMAC_KEY"
            )
            assert rows[0]["hmac_sha256"] != self._expected_digest(bare, rows[0]), (
                "digest verified with the bare AUDIT_HMAC_KEY — precedence"
                " regressed to bare-key-first"
            )
        finally:
            os.environ.pop("AUDIT_HMAC_KEY", None)
            os.environ.pop("AUDIT_HMAC_KEY_V1", None)


if __name__ == "__main__":
    unittest.main()
