"""Review-feedback pins for PR #816 (issue #690) — swarm-pr-review findings.

Covers the guards/gates the frozen acceptance checks do not pin:

- PRR-004: the ambiguity guard (two vaults whose sanitized names collide
  both skip; the shared legacy directory is consumed by neither).
- PRR-010: case-aliasing (two names differing only in case) hits the same
  guard via the lowercased keys.
- PRR-006: the ``migration.vault_paths.done`` once-gate — set on a clean
  run, NOT set when any vault's move failed, and short-circuiting the next
  invocation entirely.
- PRR-008: the orphan backfill's org-members exclusion arm (a member of an
  org that owns a vault gets no direct Default-vault row).
- PRR-011: the create route's slug-collision retry SUCCEEDS with the
  ``-2`` suffix, and the update route has the same retry.
- PRR-014: ``_SYSTEM_FLAGS_DDL`` stays byte-identical to the
  ``system_flags`` block inside the base schema (double-definition parity).
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.organizations import router as organizations_router
from app.models.database import (
    _SYSTEM_FLAGS_DDL,
    migrate_assign_orphan_users_to_default_vault,
    migrate_vault_paths,
    run_migrations,
)
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


def _vaults_db(tmp_path, rows):
    """Minimal vaults-table DB + vaults dir, mirroring the frozen harness."""
    db_path = str(tmp_path / "vaults.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE vaults (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.executemany("INSERT INTO vaults (id, name) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path, tmp_path / "vaults"


def _flag_exists(db_path, name):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM system_flags WHERE name = ?", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


class TestAmbiguityGuard:
    def test_sanitization_collision_consumes_neither_dir(self, tmp_path, monkeypatch):
        """PRR-004: two vaults whose names sanitize identically both skip."""
        monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
        db_path, vaults_dir = _vaults_db(
            tmp_path, [(1, "My Vault"), (2, "My_Vault")]
        )
        legacy = vaults_dir / "My_Vault"
        legacy.mkdir(parents=True)
        (legacy / "a.txt").write_text("legacy", encoding="utf-8")

        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = vaults_dir
            migrate_vault_paths(db_path)

        assert (legacy / "a.txt").read_text(encoding="utf-8") == "legacy"
        assert not (vaults_dir / "1").exists()
        assert not (vaults_dir / "2").exists()

    def test_case_only_name_collision_consumes_neither_dir(
        self, tmp_path, monkeypatch
    ):
        """PRR-010: names differing only in case alias onto one directory."""
        monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
        db_path, vaults_dir = _vaults_db(tmp_path, [(1, "Docs"), (2, "DOCS")])
        legacy = vaults_dir / "Docs"
        legacy.mkdir(parents=True)
        (legacy / "d.txt").write_text("docs", encoding="utf-8")

        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = vaults_dir
            migrate_vault_paths(db_path)

        assert (legacy / "d.txt").read_text(encoding="utf-8") == "docs"
        assert not (vaults_dir / "1").exists()
        assert not (vaults_dir / "2").exists()


class TestVaultPathsOnceGate:
    def test_clean_run_sets_the_gate(self, tmp_path, monkeypatch):
        """PRR-006 (a): a run with no per-vault failure records the gate."""
        monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
        db_path, vaults_dir = _vaults_db(tmp_path, [(1, "Test Vault")])
        old = vaults_dir / "Test_Vault"
        old.mkdir(parents=True)
        (old / "f.txt").write_text("x", encoding="utf-8")

        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = vaults_dir
            migrate_vault_paths(db_path)

        assert (vaults_dir / "1" / "f.txt").exists()
        assert _flag_exists(db_path, "migration.vault_paths.done")

    def test_failed_run_does_not_set_the_gate(self, tmp_path, monkeypatch):
        """PRR-006 (b): had_failure leaves the gate unset (retry next boot)."""
        monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
        db_path, vaults_dir = _vaults_db(tmp_path, [(1, "Good Vault")])
        old = vaults_dir / "Good_Vault"
        old.mkdir(parents=True)
        (old / "f.txt").write_text("x", encoding="utf-8")

        original_rename = Path.rename

        def failing_rename(path: Path, target: Path):
            if path == old:
                raise OSError("simulated rename failure")
            return original_rename(path, target)

        with patch("app.models.database.settings") as mock_settings, patch.object(
            Path, "rename", failing_rename
        ):
            mock_settings.vaults_dir = vaults_dir
            migrate_vault_paths(db_path)

        assert not _flag_exists(db_path, "migration.vault_paths.done")

    def test_gate_short_circuits_the_next_invocation(
        self, tmp_path, monkeypatch
    ):
        """PRR-006 (c): after a clean run, a later legacy layout stays put."""
        monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
        db_path, vaults_dir = _vaults_db(tmp_path, [(1, "First Vault")])
        first = vaults_dir / "First_Vault"
        first.mkdir(parents=True)
        (first / "f1.txt").write_text("1", encoding="utf-8")

        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = vaults_dir
            migrate_vault_paths(db_path)
        assert (vaults_dir / "1" / "f1.txt").exists()

        # A legacy dir that appears AFTER the gate was set (restore from
        # backup, late-arriving data) must not be name-resolved anymore.
        second = vaults_dir / "Second_Vault"
        second.mkdir(parents=True)
        (second / "f2.txt").write_text("2", encoding="utf-8")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO vaults (id, name) VALUES (2, 'Second Vault')")
        conn.commit()
        conn.close()

        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = vaults_dir
            migrate_vault_paths(db_path)

        assert (second / "f2.txt").read_text(encoding="utf-8") == "2"
        assert not (vaults_dir / "2").exists()


class TestOrgMembersExclusionArm:
    def test_org_member_of_vault_owning_org_gets_no_backfill_row(
        self, tmp_path, monkeypatch
    ):
        """PRR-008: org_members of a vault-owning org are excluded."""
        monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
        db_path = str(tmp_path / "app.db")
        run_migrations(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role,"
            " is_active) VALUES (10, 'orgmember', 'hash', 'Org Member',"
            " 'member', 1)"
        )
        conn.execute(
            "INSERT INTO organizations (name, slug) VALUES ('Own Org', 'own-org')"
        )
        org_id = conn.execute(
            "SELECT id FROM organizations WHERE slug = 'own-org'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO vaults (name, description, org_id)"
            " VALUES ('Org Vault', 'owned', ?)",
            (org_id,),
        )
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role)"
            " VALUES (?, 10, 'owner')",
            (org_id,),
        )
        conn.commit()

        migrate_assign_orphan_users_to_default_vault(db_path)

        count = conn.execute(
            "SELECT COUNT(*) FROM vault_members WHERE user_id = 10"
        ).fetchone()[0]
        conn.close()
        assert count == 0


class TestSlugCollisionRetry:
    @pytest.fixture(autouse=True)
    def setup_db(self, monkeypatch, tmp_path):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        self.db_path = str(tmp_path / "app.db")
        monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
        monkeypatch.setattr(
            "app.config.settings.jwt_secret_key",
            "test-secret-key-for-testing-only-min-32-chars!!",
        )
        monkeypatch.setattr("app.config.settings.users_enabled", True)
        run_migrations(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        pw = hash_password("testpass")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role,"
            " is_active) VALUES (2, 'admin1', ?, 'Admin One', 'admin', 1)",
            (pw,),
        )
        conn.commit()
        conn.close()

        yield

        with _pool_cache_lock:
            if self.db_path in _pool_cache:
                _pool_cache[self.db_path].close_all()
                del _pool_cache[self.db_path]

    def _client(self):
        app = FastAPI()
        app.include_router(auth_router, prefix="/api")
        app.include_router(organizations_router, prefix="/api")
        tc = TestClient(app)
        tc.headers["user-agent"] = ""
        return tc

    def _admin_headers(self):
        token = create_access_token(
            2,
            "admin1",
            "admin",
            client_fingerprint=compute_client_fingerprint(""),
        )
        return {"Authorization": f"Bearer {token}"}

    def _slug_of(self, name):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT slug FROM organizations WHERE name = ?", (name,)
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    def test_create_route_retry_succeeds_with_suffix(self):
        """PRR-011 (create): a slug collision retries once with '-2'."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO organizations (name, slug) VALUES ('First', 'org-x')"
        )
        conn.commit()
        conn.close()

        client = self._client()
        resp = client.post(
            "/api/organizations",
            json={"name": "Org X"},
            headers=self._admin_headers(),
        )
        assert resp.status_code == 200
        assert self._slug_of("Org X") == "org-x-2"

    def test_update_route_retry_succeeds_with_suffix(self):
        """PRR-011 (update): renaming into a taken slug retries with '-2'."""
        client = self._client()
        headers = self._admin_headers()
        resp = client.post(
            "/api/organizations",
            json={"name": "First"},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/organizations",
            json={"name": "Beta"},
            headers=headers,
        )
        assert resp.status_code == 200
        beta_id = resp.json()["id"]

        resp = client.patch(
            f"/api/organizations/{beta_id}",
            json={"name": "First!"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert self._slug_of("First!") == "first-2"

    def test_update_route_double_collision_still_conflicts(self):
        """PRR-011: exhausting the retry still yields the 409 contract."""
        client = self._client()
        headers = self._admin_headers()
        resp = client.post(
            "/api/organizations", json={"name": "First"}, headers=headers
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/organizations", json={"name": "Beta"}, headers=headers
        )
        assert resp.status_code == 200
        beta_id = resp.json()["id"]
        # Occupy both 'second' and 'second-2' so the suffix retry also fails.
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO organizations (name, slug) VALUES ('Gamma', 'second')"
        )
        conn.execute(
            "INSERT INTO organizations (name, slug) VALUES ('Delta', 'second-2')"
        )
        conn.commit()
        conn.close()

        resp = client.patch(
            f"/api/organizations/{beta_id}",
            json={"name": "Second"},
            headers=headers,
        )
        assert resp.status_code == 409
        assert self._slug_of("Beta") == "beta"


def test_system_flags_ddl_stays_in_parity_with_base_schema():
    """PRR-014: the helper DDL and the _BASE_SCHEMA block must not drift."""
    import re

    from app.models import database

    match = re.search(
        r"CREATE TABLE IF NOT EXISTS system_flags \(.*?\);",
        database._BASE_SCHEMA,
        re.DOTALL,
    )
    assert match is not None
    assert match.group(0) == database._SYSTEM_FLAGS_DDL.strip()
