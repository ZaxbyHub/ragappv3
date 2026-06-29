"""
Tests for per-vault and per-file enrichment toggles (FR-006, SC-013).

Tests:
1. Vault toggle API sets vault override correctly
2. Override ON -> is_enrichment_enabled_for_vault returns True even if global OFF
3. Override OFF -> is_enrichment_enabled_for_vault returns False even if global ON
4. NULL -> inherits global setting
5. Authz: only admin/owner can toggle vault (viewer/member get 403)
6. Per-file override ON -> ignores vault and global settings
7. Per-file override OFF -> ignores vault and global settings
8. Per-file NULL -> inherits vault setting
9. Clear file override -> inherits vault setting
10. Per-file authz: only admin/owner can toggle file (viewer/member get 403)
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import TEST_SCHEMA
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.documents import router as documents_router
from app.api.routes.vaults import router as vaults_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Set up test database with full schema and seed data."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    # Clear pool cache BEFORE setting up new database
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    # Initialize full schema (includes files/memories/chat_sessions for vault count queries)
    from app.models.database import init_db
    init_db(db_path)

    # Patch settings
    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    # Seed test users
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "superadmin", pw, "Super Admin", "superadmin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (2, "admin1", pw, "Admin One", "admin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (3, "member1", pw, "Member One", "member"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (4, "viewer1", pw, "Viewer One", "viewer"),
    )
    conn.commit()
    conn.close()

    # Build a minimal FastAPI app with just the vaults router + documents router + auth dependencies
    test_app = FastAPI()
    test_app.include_router(auth_router, prefix="/api/auth")
    test_app.include_router(vaults_router, prefix="/api")
    test_app.include_router(documents_router, prefix="/api")

    # Inject test DB pool
    from _db_pool import SimpleConnectionPool

    pool = SimpleConnectionPool(db_path)

    def override_get_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    from app.api.deps import get_db
    from app.security import csrf_protect

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    # Provide a real JWT for authenticated requests
    def override_current_active_user():
        return {
            "id": 1,
            "username": "superadmin",
            "full_name": "Super Admin",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }

    from app.api.deps import get_current_active_user

    test_app.dependency_overrides[get_current_active_user] = override_current_active_user

    yield test_app, db_path, pool, monkeypatch

    # Cleanup
    pool.close_all()


def auth_headers(user_id: int, role: str) -> dict:
    """Return headers with a valid JWT for the given user."""
    token = create_access_token(
        user_id=user_id,
        username=["superadmin", "admin1", "member1", "viewer1"][user_id - 1],
        role=role,
        client_fingerprint=compute_client_fingerprint(""),
    )
    return {"Authorization": f"Bearer {token}", "X-CSRF-Token": "test-csrf-token"}


class TestEnrichmentToggleAPI:
    """Tests for PUT /api/vaults/{vault_id}/enrichment-toggle."""

    def test_toggle_on_sets_vault_override(self, setup_db):
        """PUT with enabled=true sets enrichment_enabled=1 on the vault."""
        test_app, db_path, pool, _ = setup_db

        # Create vault as superadmin
        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "Test Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Toggle enrichment ON
        resp = client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_enabled"] is True
        # effective_enrichment_enabled should be True since override is set
        assert data["effective_enrichment_enabled"] is True

        # Verify DB
        conn = pool.get_connection()
        row = conn.execute(
            "SELECT enrichment_enabled FROM vaults WHERE id = ?", (vault_id,)
        ).fetchone()
        pool.release_connection(conn)
        assert row[0] == 1

    def test_toggle_off_sets_vault_override(self, setup_db):
        """PUT with enabled=false sets enrichment_enabled=0 on the vault."""
        test_app, db_path, pool, _ = setup_db

        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "Test Vault 2"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        resp = client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": False},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_enabled"] is False
        assert data["effective_enrichment_enabled"] is False

        conn = pool.get_connection()
        row = conn.execute(
            "SELECT enrichment_enabled FROM vaults WHERE id = ?", (vault_id,)
        ).fetchone()
        pool.release_connection(conn)
        assert row[0] == 0

    def test_toggle_null_clears_override(self, setup_db):
        """PUT with enabled=null clears the vault override (NULL)."""
        test_app, db_path, pool, _ = setup_db

        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "Test Vault 3"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # First set an override
        resp = client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        assert resp.json()["enrichment_enabled"] is True

        # Clear override with null
        resp = client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": None},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_enabled"] is None

        # Verify DB
        conn = pool.get_connection()
        row = conn.execute(
            "SELECT enrichment_enabled FROM vaults WHERE id = ?", (vault_id,)
        ).fetchone()
        pool.release_connection(conn)
        assert row[0] is None

    def test_vault_not_found(self, setup_db):
        """PUT returns 404 for non-existent vault."""
        test_app, db_path, pool, _ = setup_db
        client = TestClient(test_app)
        resp = client.put(
            "/api/vaults/99999/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 404

    def test_non_admin_gets_403(self, setup_db):
        """PUT returns 403 when caller is not admin/owner of vault."""
        test_app, db_path, pool, monkeypatch = setup_db

        client = TestClient(test_app)

        # Create vault as superadmin
        resp = client.post(
            "/api/vaults",
            json={"name": "Admin Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Give member1 admin permission on the vault
        conn = pool.get_connection()
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (vault_id, 3, "admin"),
        )
        conn.commit()
        pool.release_connection(conn)

        # member1 (role=member) should be able to toggle (they're admin on this vault)
        resp = client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(3, "member"),
        )
        # require_vault_permission("admin") allows members with admin permission
        assert resp.status_code == 200

    def test_viewer_cannot_toggle(self, setup_db):
        """PUT returns 403 when caller is a viewer on the vault."""
        test_app, db_path, pool, monkeypatch = setup_db

        client = TestClient(test_app)

        # Create vault as superadmin
        resp = client.post(
            "/api/vaults",
            json={"name": "Viewer Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Give viewer1 read permission on the vault
        conn = pool.get_connection()
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (vault_id, 4, "read"),
        )
        conn.commit()
        pool.release_connection(conn)

        # Override get_current_active_user to return viewer1 (not superadmin)
        # so require_vault_permission("admin") actually checks viewer1's permission
        from app.api.deps import get_current_active_user

        def override_viewer_user():
            return {
                "id": 4,
                "username": "viewer1",
                "full_name": "Viewer One",
                "role": "viewer",
                "is_active": True,
                "must_change_password": False,
            }

        test_app.dependency_overrides[get_current_active_user] = override_viewer_user

        try:
            resp = client.put(
                f"/api/vaults/{vault_id}/enrichment-toggle",
                json={"enabled": True},
                headers=auth_headers(4, "viewer"),
            )
            assert resp.status_code == 403
        finally:
            # Restore superadmin override
            def override_superadmin_user():
                return {
                    "id": 1,
                    "username": "superadmin",
                    "full_name": "Super Admin",
                    "role": "superadmin",
                    "is_active": True,
                    "must_change_password": False,
                }

            test_app.dependency_overrides[get_current_active_user] = override_superadmin_user


class TestIsEnrichmentEnabledForVault:
    """Tests for is_enrichment_enabled_for_vault() helper."""

    def test_null_vault_id_uses_global(self, setup_db):
        """When vault_id is None, helper uses global settings."""
        test_app, db_path, pool, monkeypatch = setup_db

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_vault

        # Global is OFF by default
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_vault(None) is False

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        assert is_enrichment_enabled_for_vault(None) is True

    def test_vault_null_override_uses_global(self, setup_db):
        """When vault enrichment_enabled is NULL, helper uses global."""
        test_app, db_path, pool, monkeypatch = setup_db

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_vault

        # Create vault without setting override
        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "Global Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_vault(vault_id) is False

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        assert is_enrichment_enabled_for_vault(vault_id) is True

    def test_vault_override_on_ignores_global(self, setup_db):
        """When vault override is ON, helper returns True regardless of global."""
        test_app, db_path, pool, monkeypatch = setup_db

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_vault

        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "On Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Set vault override ON
        client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )

        # Even with global OFF, vault override should win
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_vault(vault_id) is True

    def test_vault_override_off_ignores_global(self, setup_db):
        """When vault override is OFF, helper returns False regardless of global."""
        test_app, db_path, pool, monkeypatch = setup_db

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_vault

        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "Off Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Set vault override OFF
        client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": False},
            headers=auth_headers(1, "superadmin"),
        )

        # Even with global ON, vault override should win
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        assert is_enrichment_enabled_for_vault(vault_id) is False

    def test_vault_not_found_returns_global(self, setup_db):
        """When vault does not exist, helper falls back to global."""
        test_app, db_path, pool, monkeypatch = setup_db

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_vault

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        assert is_enrichment_enabled_for_vault(99999) is True

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_vault(99999) is False


class TestEffectiveEnrichmentInVaultResponse:
    """Tests that VaultResponse includes effective_enrichment_enabled."""

    def test_get_vault_returns_effective_enrichment(self, setup_db):
        """GET /api/vaults/{id} returns effective_enrichment_enabled."""
        test_app, db_path, pool, monkeypatch = setup_db

        from app.config import settings

        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "Effective Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Default (no override) - should inherit global
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        resp = client.get(f"/api/vaults/{vault_id}", headers=auth_headers(1, "superadmin"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_enabled"] is None
        assert data["effective_enrichment_enabled"] is True

        # Set override ON
        client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )
        resp = client.get(f"/api/vaults/{vault_id}", headers=auth_headers(1, "superadmin"))
        data = resp.json()
        assert data["enrichment_enabled"] is True
        assert data["effective_enrichment_enabled"] is True

        # Set override OFF (global still ON)
        client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": False},
            headers=auth_headers(1, "superadmin"),
        )
        resp = client.get(f"/api/vaults/{vault_id}", headers=auth_headers(1, "superadmin"))
        data = resp.json()
        assert data["enrichment_enabled"] is False
        assert data["effective_enrichment_enabled"] is False

    def test_list_vaults_returns_effective_enrichment(self, setup_db):
        """GET /api/vaults returns vaults with effective_enrichment_enabled."""
        test_app, db_path, pool, monkeypatch = setup_db

        from app.config import settings

        client = TestClient(test_app)
        resp = client.post(
            "/api/vaults",
            json={"name": "List Vault 1"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Set override OFF on the vault
        client.put(
            f"/api/vaults/{vault_id}/enrichment-toggle",
            json={"enabled": False},
            headers=auth_headers(1, "superadmin"),
        )

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        resp = client.get("/api/vaults", headers=auth_headers(1, "superadmin"))
        assert resp.status_code == 200
        vaults = resp.json()["vaults"]
        target = next(v for v in vaults if v["id"] == vault_id)
        assert target["enrichment_enabled"] is False
        assert target["effective_enrichment_enabled"] is False


class TestFileEnrichmentToggleAPI:
    """Tests for PUT /api/documents/files/{file_id}/enrichment-toggle."""

    def _create_vault_and_file(self, setup_db):
        """Create a vault and a file for per-file toggle tests."""
        test_app, db_path, pool, monkeypatch = setup_db
        client = TestClient(test_app)

        # Create vault
        resp = client.post(
            "/api/vaults",
            json={"name": "File Toggle Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Create a file directly in the database
        conn = pool.get_connection()
        cursor = conn.execute(
            """
            INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, file_type, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (vault_id, "/tmp/test.txt", "test.txt", "abc123", 100, "text/plain", "indexed"),
        )
        conn.commit()
        file_id = cursor.lastrowid
        pool.release_connection(conn)

        return test_app, db_path, pool, monkeypatch, client, vault_id, file_id

    def test_toggle_file_on_sets_override(self, setup_db):
        """PUT with enabled=true sets enrichment_enabled=1 on the file."""
        test_app, db_path, pool, monkeypatch, client, vault_id, file_id = self._create_vault_and_file(setup_db)

        resp = client.put(
            f"/api/documents/{file_id}/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_enabled"] is True
        assert data["effective_enrichment_enabled"] is True

        # Verify DB
        conn = pool.get_connection()
        row = conn.execute(
            "SELECT enrichment_enabled FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        pool.release_connection(conn)
        assert row[0] == 1

    def test_toggle_file_off_sets_override(self, setup_db):
        """PUT with enabled=false sets enrichment_enabled=0 on the file."""
        test_app, db_path, pool, monkeypatch, client, vault_id, file_id = self._create_vault_and_file(setup_db)

        resp = client.put(
            f"/api/documents/{file_id}/enrichment-toggle",
            json={"enabled": False},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_enabled"] is False
        assert data["effective_enrichment_enabled"] is False

        # Verify DB
        conn = pool.get_connection()
        row = conn.execute(
            "SELECT enrichment_enabled FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        pool.release_connection(conn)
        assert row[0] == 0

    def test_toggle_file_null_clears_override(self, setup_db):
        """PUT with enabled=null clears the file override (NULL)."""
        test_app, db_path, pool, monkeypatch, client, vault_id, file_id = self._create_vault_and_file(setup_db)

        # First set an override
        resp = client.put(
            f"/api/documents/{file_id}/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        assert resp.json()["enrichment_enabled"] is True

        # Clear override with null
        resp = client.put(
            f"/api/documents/{file_id}/enrichment-toggle",
            json={"enabled": None},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrichment_enabled"] is None

        # Verify DB
        conn = pool.get_connection()
        row = conn.execute(
            "SELECT enrichment_enabled FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        pool.release_connection(conn)
        assert row[0] is None

    def test_file_not_found(self, setup_db):
        """PUT returns 404 for non-existent file."""
        test_app, db_path, pool, monkeypatch, client, vault_id, file_id = self._create_vault_and_file(setup_db)

        resp = client.put(
            "/api/documents/99999/enrichment-toggle",
            json={"enabled": True},
            headers=auth_headers(1, "superadmin"),
        )
        assert resp.status_code == 404

    def test_viewer_cannot_toggle_file(self, setup_db):
        """PUT returns 403 when viewer has only read permission on vault."""
        test_app, db_path, pool, monkeypatch, client, vault_id, file_id = self._create_vault_and_file(setup_db)

        # Give viewer1 read permission on the vault
        conn = pool.get_connection()
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (vault_id, 4, "read"),
        )
        conn.commit()
        pool.release_connection(conn)

        # Override get_current_active_user to return viewer1
        from app.api.deps import get_current_active_user

        def override_viewer_user():
            return {
                "id": 4,
                "username": "viewer1",
                "full_name": "Viewer One",
                "role": "viewer",
                "is_active": True,
                "must_change_password": False,
            }

        test_app.dependency_overrides[get_current_active_user] = override_viewer_user

        try:
            resp = client.put(
                f"/api/documents/{file_id}/enrichment-toggle",
                json={"enabled": True},
                headers=auth_headers(4, "viewer"),
            )
            assert resp.status_code == 403
        finally:
            # Restore superadmin override
            def override_superadmin_user():
                return {
                    "id": 1,
                    "username": "superadmin",
                    "full_name": "Super Admin",
                    "role": "superadmin",
                    "is_active": True,
                    "must_change_password": False,
                }

            test_app.dependency_overrides[get_current_active_user] = override_superadmin_user

    def test_vault_admin_can_toggle_file(self, setup_db):
        """PUT allows vault admin to toggle file enrichment."""
        test_app, db_path, pool, monkeypatch, client, vault_id, file_id = self._create_vault_and_file(setup_db)

        # Give member1 admin permission on the vault
        conn = pool.get_connection()
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (vault_id, 3, "admin"),
        )
        conn.commit()
        pool.release_connection(conn)

        # Override get_current_active_user to return member1
        from app.api.deps import get_current_active_user

        def override_member_user():
            return {
                "id": 3,
                "username": "member1",
                "full_name": "Member One",
                "role": "member",
                "is_active": True,
                "must_change_password": False,
            }

        test_app.dependency_overrides[get_current_active_user] = override_member_user

        try:
            resp = client.put(
                f"/api/documents/{file_id}/enrichment-toggle",
                json={"enabled": True},
                headers=auth_headers(3, "member"),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["enrichment_enabled"] is True
        finally:
            # Restore superadmin override
            def override_superadmin_user():
                return {
                    "id": 1,
                    "username": "superadmin",
                    "full_name": "Super Admin",
                    "role": "superadmin",
                    "is_active": True,
                    "must_change_password": False,
                }

            test_app.dependency_overrides[get_current_active_user] = override_superadmin_user


class TestIsEnrichmentEnabledForFile:
    """Tests for is_enrichment_enabled_for_file() helper."""

    def _create_vault_and_file(self, setup_db):
        """Create a vault and a file for file-level resolution tests."""
        test_app, db_path, pool, monkeypatch = setup_db
        client = TestClient(test_app)

        # Create vault
        resp = client.post(
            "/api/vaults",
            json={"name": "File Resolution Vault"},
            headers=auth_headers(1, "superadmin"),
        )
        vault_id = resp.json()["id"]

        # Create a file directly in the database
        conn = pool.get_connection()
        cursor = conn.execute(
            """
            INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, file_type, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (vault_id, "/tmp/test2.txt", "test2.txt", "def456", 200, "text/plain", "indexed"),
        )
        conn.commit()
        file_id = cursor.lastrowid
        pool.release_connection(conn)

        return test_app, db_path, pool, monkeypatch, vault_id, file_id

    def test_file_override_on_ignores_vault_and_global(self, setup_db):
        """When file override is ON, helper returns True regardless of vault/global."""
        test_app, db_path, pool, monkeypatch, vault_id, file_id = self._create_vault_and_file(setup_db)

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_file

        # Set file override ON
        conn = pool.get_connection()
        conn.execute(
            "UPDATE files SET enrichment_enabled = 1 WHERE id = ?", (file_id,)
        )
        # Set vault override OFF
        conn.execute(
            "UPDATE vaults SET enrichment_enabled = 0 WHERE id = ?", (vault_id,)
        )
        conn.commit()
        pool.release_connection(conn)

        # Global OFF, vault OFF, but file ON -> should return True
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_file(file_id, vault_id) is True

        # Even with vault ON, file ON should still win
        conn = pool.get_connection()
        conn.execute(
            "UPDATE vaults SET enrichment_enabled = 1 WHERE id = ?", (vault_id,)
        )
        conn.commit()
        pool.release_connection(conn)
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_file(file_id, vault_id) is True

    def test_file_override_off_ignores_vault_and_global(self, setup_db):
        """When file override is OFF, helper returns False regardless of vault/global."""
        test_app, db_path, pool, monkeypatch, vault_id, file_id = self._create_vault_and_file(setup_db)

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_file

        # Set file override OFF
        conn = pool.get_connection()
        conn.execute(
            "UPDATE files SET enrichment_enabled = 0 WHERE id = ?", (file_id,)
        )
        # Set vault override ON
        conn.execute(
            "UPDATE vaults SET enrichment_enabled = 1 WHERE id = ?", (vault_id,)
        )
        conn.commit()
        pool.release_connection(conn)

        # Global ON, vault ON, but file OFF -> should return False
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        assert is_enrichment_enabled_for_file(file_id, vault_id) is False

    def test_file_null_inherits_vault(self, setup_db):
        """When file enrichment_enabled is NULL, helper uses vault setting."""
        test_app, db_path, pool, monkeypatch, vault_id, file_id = self._create_vault_and_file(setup_db)

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_file

        # Ensure file override is NULL
        conn = pool.get_connection()
        conn.execute(
            "UPDATE files SET enrichment_enabled = NULL WHERE id = ?", (file_id,)
        )
        # Set vault override ON
        conn.execute(
            "UPDATE vaults SET enrichment_enabled = 1 WHERE id = ?", (vault_id,)
        )
        conn.commit()
        pool.release_connection(conn)

        # File NULL, vault ON -> should return True (inherit vault)
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_file(file_id, vault_id) is True

        # Set vault override OFF
        conn = pool.get_connection()
        conn.execute(
            "UPDATE vaults SET enrichment_enabled = 0 WHERE id = ?", (vault_id,)
        )
        conn.commit()
        pool.release_connection(conn)
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        assert is_enrichment_enabled_for_file(file_id, vault_id) is False

    def test_clear_file_override_inherits_vault(self, setup_db):
        """Clearing file override (NULL) makes it inherit vault setting."""
        test_app, db_path, pool, monkeypatch, vault_id, file_id = self._create_vault_and_file(setup_db)

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_file

        # Set file override ON first
        conn = pool.get_connection()
        conn.execute(
            "UPDATE files SET enrichment_enabled = 1 WHERE id = ?", (file_id,)
        )
        # Set vault override OFF
        conn.execute(
            "UPDATE vaults SET enrichment_enabled = 0 WHERE id = ?", (vault_id,)
        )
        conn.commit()
        pool.release_connection(conn)

        # File ON, vault OFF -> True
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        assert is_enrichment_enabled_for_file(file_id, vault_id) is True

        # Clear file override (set to NULL)
        conn = pool.get_connection()
        conn.execute(
            "UPDATE files SET enrichment_enabled = NULL WHERE id = ?", (file_id,)
        )
        conn.commit()
        pool.release_connection(conn)

        # File NULL, vault OFF -> False (inherit vault)
        assert is_enrichment_enabled_for_file(file_id, vault_id) is False

    def test_file_not_found_returns_vault_fallback(self, setup_db):
        """When file does not exist, helper falls back to vault resolution."""
        test_app, db_path, pool, monkeypatch, vault_id, file_id = self._create_vault_and_file(setup_db)

        from app.config import settings
        from app.services.document_processor import is_enrichment_enabled_for_file

        # Vault ON, file doesn't exist
        conn = pool.get_connection()
        conn.execute(
            "UPDATE vaults SET enrichment_enabled = 1 WHERE id = ?", (vault_id,)
        )
        conn.commit()
        pool.release_connection(conn)

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", False)
        # Non-existent file_id falls back to vault
        assert is_enrichment_enabled_for_file(99999, vault_id) is True

        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)
        assert is_enrichment_enabled_for_file(99999, vault_id) is True

