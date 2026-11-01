"""Arm's-length acceptance checks for issue #723 (D05): vault-delete integrity.

Six discriminating checks authored against master 4323afbb. Every test here is
RED at base for the defect assertion (never a harness error) and turns GREEN
after the fix:

* ``test_vault_delete_leaves_no_orphan_rows`` — vault-scoped rows without an
  FK to ``vaults`` (kms_entries / wiki_entities / wiki_relations /
  document_reindex_jobs) survive ``DELETE /api/vaults/{id}``.
* ``test_vault_delete_cancels_pending_compile_jobs`` — pending wiki/kms
  compile jobs stay ``pending`` after the vault is deleted.
* ``test_vault_counts_query_cost_is_not_a_cross_product`` — the
  vault-with-counts LEFT JOIN triple materializes an F*M*S cross product
  (progress-handler tick budget blown at 40/40/40).
* ``test_vector_tombstones_durable_before_post_commit_cancel`` — an
  ``asyncio.CancelledError`` after the relational commit leaves zero durable
  ``vector_delete_pending`` tombstones.
* ``test_fresh_tombstone_survives_startup_reconcile`` — a tombstoned draft
  input that belongs to a surviving draft is age-deleted by startup
  reconcile because ``os.replace`` preserved its back-dated mtime.
* ``test_draft_inputs_restored_on_unlisted_exception`` — a ``ValueError``
  from ``devault_asset_rel_paths`` escapes both restoring handlers, leaving
  the tombstoned draft bytes stranded after rollback.

Production code is untouched by this file: acceptance checks only, no fixes.
The harness patterns (env vars, ``SimpleConnectionPool``, TestClient +
dependency overrides including csrf_protect, direct ``delete_vault`` calls,
draft seeding) mirror the verified probe at
``.agents/issue-traces/723-vault-delete-integrity/repro/probe-723.py`` and
``backend/tests/test_vault_delete_cascade_membership.py``.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The harness installs its own csrf_protect dependency override below; the
# declaration keeps the conftest autouse CSRF bypass off this module.
CSRF_TEST_POLICY = "naive"

import pytest  # noqa: E402
from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.api.deps import get_current_active_user, get_db, get_vector_store  # noqa: E402
from app.api.routes.vaults import _fetch_vault_with_counts, delete_vault  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import init_db  # noqa: E402
from app.security import csrf_protect  # noqa: E402
from app.services.draft_input_storage import DraftInputStorage  # noqa: E402
from app.services.draft_store import DraftStore  # noqa: E402

_ADMIN_USER = {
    "id": 1,
    "username": "admin",
    "full_name": "Admin",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


class _FakeUpload:
    """Minimal async-file object accepted by ``DraftInputStorage.stage_upload``."""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk, self._offset = self._content[self._offset :], len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _build_app_pool(temp_dir: str) -> SimpleConnectionPool:
    """Create a fresh app DB + pool seeded with the admin user row."""
    db_path = str(Path(temp_dir) / "test.db")
    init_db(db_path)
    pool = SimpleConnectionPool(db_path)
    conn = pool.get_connection()
    try:
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "admin", "abc123", "superadmin", 1),
        )
        conn.commit()
    finally:
        pool.release_connection(conn)
    return pool


def _install_route_overrides(pool: SimpleConnectionPool, mock_vector_store) -> None:
    """Install the shared dependency overrides (membership-test setUp mirror)."""

    def override_get_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_vector_store] = lambda: mock_vector_store
    app.dependency_overrides[get_current_active_user] = lambda: dict(_ADMIN_USER)
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf"


def _clear_route_overrides() -> None:
    """Pop every override installed by ``_install_route_overrides``."""
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_vector_store, None)
    app.dependency_overrides.pop(get_current_active_user, None)
    app.dependency_overrides.pop(csrf_protect, None)


def _delete_scope(vault_id: int) -> dict:
    """Minimal ASGI scope for calling ``delete_vault`` directly (probe pattern)."""
    return {
        "type": "http",
        "method": "DELETE",
        "headers": [],
        "path": f"/api/vaults/{vault_id}",
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }


def _vault_row_count(conn, table: str, vault_id: int) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE vault_id = ?", (vault_id,)
    ).fetchone()[0]


def _seed_scoped_row(conn, table: str, vault_id: int) -> None:
    """Insert exactly one row for the parametrized table (probe insert shapes)."""
    if table == "kms_entries":
        conn.execute(
            "INSERT INTO kms_entries (vault_id, slug, title, body, summary, "
            "tags_json, source_type, status) "
            "VALUES (?, 'probe', 'Probe', 'body', '', '[]', 'manual', 'draft')",
            (vault_id,),
        )
    elif table == "wiki_entities":
        conn.execute(
            "INSERT INTO wiki_entities (vault_id, canonical_name) VALUES (?, 'Alpha')",
            (vault_id,),
        )
    elif table == "wiki_relations":
        claim_id = conn.execute(
            "INSERT INTO wiki_claims (vault_id, claim_text, source_type) "
            "VALUES (?, 'probe claim', 'manual')",
            (vault_id,),
        ).lastrowid
        ent1 = conn.execute(
            "INSERT INTO wiki_entities (vault_id, canonical_name) VALUES (?, 'Alpha')",
            (vault_id,),
        ).lastrowid
        ent2 = conn.execute(
            "INSERT INTO wiki_entities (vault_id, canonical_name) VALUES (?, 'Beta')",
            (vault_id,),
        ).lastrowid
        conn.execute(
            "INSERT INTO wiki_relations (vault_id, claim_id, subject_entity_id, "
            "predicate, object_entity_id) VALUES (?, ?, ?, 'relates_to', ?)",
            (vault_id, claim_id, ent1, ent2),
        )
    elif table == "document_reindex_jobs":
        conn.execute(
            "INSERT INTO document_reindex_jobs (vault_id, trigger_type, status, "
            "input_json) VALUES (?, 'api', 'pending', '{}')",
            (vault_id,),
        )
    else:  # pragma: no cover - parametrize list is exhaustive
        raise AssertionError(f"unknown table: {table}")


class _RouteEnv:
    """Client + pool handle for the module-level route tests."""

    def __init__(self, client, pool):
        self.client = client
        self.pool = pool

    def create_vault(self, name: str) -> int:
        resp = self.client.post("/api/vaults", json={"name": name, "description": ""})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]


@pytest.fixture()
def route_env():
    """Fresh app DB/pool/overrides per test (probe ``_seed_env`` pattern)."""
    temp_dir = tempfile.mkdtemp()
    original_data_dir = settings.data_dir
    settings.data_dir = Path(temp_dir)
    pool = _build_app_pool(temp_dir)
    mock_vector_store = MagicMock()
    mock_vector_store.delete_by_vault = AsyncMock(return_value=0)
    _install_route_overrides(pool, mock_vector_store)
    try:
        yield _RouteEnv(TestClient(app), pool)
    finally:
        _clear_route_overrides()
        pool.close_all()
        settings.data_dir = original_data_dir
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.parametrize(
    "table",
    ["kms_entries", "wiki_entities", "wiki_relations", "document_reindex_jobs"],
)
def test_vault_delete_leaves_no_orphan_rows(route_env, table):
    """DELETE /api/vaults/{id} leaves zero rows in vault-scoped FK-less tables.

    RED at base (delete_vault has no statement for these tables, so the one
    seeded row survives the 200 response); GREEN after the fix (the delete
    transaction removes every vault-scoped row).
    """
    vault_id = route_env.create_vault(f"D05Orphans-{table}")
    conn = route_env.pool.get_connection()
    try:
        _seed_scoped_row(conn, table, vault_id)
        conn.commit()
        seeded = _vault_row_count(conn, table, vault_id)
        assert seeded == 1
    finally:
        route_env.pool.release_connection(conn)

    resp = route_env.client.delete(f"/api/vaults/{vault_id}")
    assert resp.status_code == 200

    conn = route_env.pool.get_connection()
    try:
        orphaned = _vault_row_count(conn, table, vault_id)
        assert orphaned == 0
    finally:
        route_env.pool.release_connection(conn)


@pytest.mark.parametrize("table", ["wiki_compile_jobs", "kms_compile_jobs"])
def test_vault_delete_cancels_pending_compile_jobs(route_env, table):
    """DELETE /api/vaults/{id} leaves zero pending compile jobs behind.

    RED at base (the seeded manual/pending job is never cancelled or removed,
    so it stays pending after the 200 response); GREEN after the fix (the
    delete path settles pending compile jobs for the vault).
    """
    vault_id = route_env.create_vault(f"D05PendingJobs-{table}")
    conn = route_env.pool.get_connection()
    try:
        conn.execute(
            f"INSERT INTO {table} (vault_id, trigger_type, status) "
            "VALUES (?, 'manual', 'pending')",
            (vault_id,),
        )
        conn.commit()
        seeded = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE vault_id = ? AND status = 'pending'",
            (vault_id,),
        ).fetchone()[0]
        assert seeded == 1
    finally:
        route_env.pool.release_connection(conn)

    resp = route_env.client.delete(f"/api/vaults/{vault_id}")
    assert resp.status_code == 200

    conn = route_env.pool.get_connection()
    try:
        pending = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE vault_id = ? AND status = 'pending'",
            (vault_id,),
        ).fetchone()[0]
        assert pending == 0
    finally:
        route_env.pool.release_connection(conn)


def test_fresh_tombstone_survives_startup_reconcile():
    """A tombstoned input of a surviving draft must survive startup reconcile.

    RED at base (tombstone's os.replace preserves the back-dated mtime, so
    _reconcile_age_dir deletes the .trash entry by age even though its owning
    draft is in the valid set); GREEN after the fix (a fresh tombstone whose
    owner/draft pair survives is kept and its bytes remain reachable).
    """
    temp_dir = tempfile.mkdtemp()
    try:
        storage = DraftInputStorage(Path(temp_dir))
        staged = asyncio.run(
            storage.stage_upload(
                _FakeUpload("note.txt", b"keep me"),
                allowed_extensions={".txt"},
                max_file_bytes=1_000_000,
            )
        )
        relpath = f"7/9/inputs/1/{staged.stored_name}"
        storage.finalize(staged, relpath)

        max_age = 10.0
        back_dated = time.time() - 2 * max_age
        os.utime(storage.resolve(relpath), (back_dated, back_dated))

        token = storage.tombstone(relpath)
        assert token

        counts = storage.reconcile({(7, 9)}, max_age_seconds=max_age)
        assert counts["trash_removed"] == 0
        assert storage.exists(relpath)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class TestVaultDeleteIntegrityDirect(unittest.TestCase):
    """Direct-call checks against vaults.py (counts query + delete_vault)."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self._temp_dir)
        self._pool = _build_app_pool(self._temp_dir)
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = AsyncMock(return_value=0)
        _install_route_overrides(self._pool, self._mock_vector_store)
        self.client = TestClient(app)

    def tearDown(self):
        _clear_route_overrides()
        self._pool.close_all()
        settings.data_dir = self._original_data_dir
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _create_vault(self, name: str) -> int:
        resp = self.client.post("/api/vaults", json={"name": name, "description": ""})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def _seed_files(self, vault_id: int, count: int) -> list:
        """Insert ``count`` indexed file rows; returns their ids (committed)."""
        conn = self._pool.get_connection()
        try:
            file_ids = [
                conn.execute(
                    "INSERT INTO files (file_name, file_path, status, vault_id, "
                    "file_size) VALUES (?, ?, 'indexed', ?, 10)",
                    (f"f{i}.txt", f"/tmp/f{i}.txt", vault_id),
                ).lastrowid
                for i in range(count)
            ]
            conn.commit()
            return file_ids
        finally:
            self._pool.release_connection(conn)

    def test_vault_counts_query_cost_is_not_a_cross_product(self):
        """The vault-with-counts query must not blow a linear work budget.

        RED at base (the F*M*S LEFT JOIN cross product at 40/40/40 costs
        ~911 progress-handler ticks, far over the 100-tick budget, while the
        returned counts are correct); GREEN after the fix (a rewrite without
        the cross product returns the same counts well under budget).
        """
        vault_id = self._create_vault("D05CrossProduct")
        conn = self._pool.get_connection()
        ticks = []
        try:
            for i in range(40):
                conn.execute(
                    "INSERT INTO files (file_name, file_path, status, vault_id, "
                    "file_size) VALUES (?, ?, 'indexed', ?, 10)",
                    (f"f{i}.txt", f"/tmp/f{i}.txt", vault_id),
                )
                conn.execute(
                    "INSERT INTO memories (content, vault_id) VALUES (?, ?)",
                    (f"m{i}", vault_id),
                )
                conn.execute(
                    "INSERT INTO chat_sessions (title, vault_id) VALUES (?, ?)",
                    (f"s{i}", vault_id),
                )
            conn.commit()

            conn.set_progress_handler(lambda: ticks.append(1), 1000)
            try:
                vault = asyncio.run(_fetch_vault_with_counts(conn, vault_id))
            finally:
                conn.set_progress_handler(None, 0)

            counts = (vault.file_count, vault.memory_count, vault.session_count)
            assert counts == (40, 40, 40)
            assert len(ticks) < 100
        finally:
            conn.set_progress_handler(None, 0)
            self._pool.release_connection(conn)

    def test_vector_tombstones_durable_before_post_commit_cancel(self):
        """A post-commit CancelledError must leave per-file durable tombstones.

        RED at base (CancelledError escapes the except Exception arm, so zero
        vector_delete_pending rows exist although the vault commit already
        happened); GREEN after the fix (one durable tombstone per file id is
        recorded around the vector reconciliation).
        """
        vault_id = self._create_vault("D05CancelWindow")
        file_ids = self._seed_files(vault_id, 2)

        vector_store = MagicMock()
        vector_store.delete_by_vault = AsyncMock(side_effect=asyncio.CancelledError())

        conn = self._pool.get_connection()
        try:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(
                    delete_vault(
                        vault_id,
                        Request(_delete_scope(vault_id)),
                        conn,
                        vector_store,
                        dict(_ADMIN_USER),
                        "test-csrf",
                    )
                )

            # Post-commit window precondition: the relational delete already
            # committed, so nothing can bring the vault row back.
            vault_rows = conn.execute(
                "SELECT COUNT(*) FROM vaults WHERE id = ?", (vault_id,)
            ).fetchone()[0]
            assert vault_rows == 0

            pending = conn.execute(
                "SELECT COUNT(*) FROM vector_delete_pending WHERE file_id IN (?, ?)",
                (file_ids[0], file_ids[1]),
            ).fetchone()[0]
            assert pending == 2
        finally:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            self._pool.release_connection(conn)

    def test_draft_inputs_restored_on_unlisted_exception(self):
        """An exception outside the listed handlers must still restore bytes.

        RED at base (a ValueError from devault_asset_rel_paths escapes both
        restoring handlers, so after the rollback the tombstoned input bytes
        are not at their original path); GREEN after the fix (the restore
        runs for any failure, putting the bytes back).
        """
        vault_id = self._create_vault("D05UnlistedExc")
        storage = DraftInputStorage(Path(self._temp_dir) / "draft-room")
        conn = self._pool.get_connection()
        try:
            draft_store = DraftStore(conn)
            draft = draft_store.create_draft(
                vault_id=vault_id,
                created_by=1,
                title="d05 draft",
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            staged = asyncio.run(
                storage.stage_upload(
                    _FakeUpload("a.txt", b"private manuscript bytes"),
                    allowed_extensions={".txt"},
                    max_file_bytes=1_000_000,
                )
            )
            record = draft_store.reserve_input(
                draft_id=draft.id,
                owner_id=1,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
            storage.finalize(staged, record.storage_relpath)
            conn.commit()
            relpath = record.storage_relpath
        finally:
            self._pool.release_connection(conn)

        vector_store = MagicMock()
        vector_store.delete_by_vault = AsyncMock(return_value=0)

        conn = self._pool.get_connection()
        try:
            from app.services import artifact_store as _artifact_store

            with patch.object(
                _artifact_store,
                "devault_asset_rel_paths",
                side_effect=ValueError("simulated unlisted failure"),
            ):
                with pytest.raises(Exception):
                    asyncio.run(
                        delete_vault(
                            vault_id,
                            Request(_delete_scope(vault_id)),
                            conn,
                            vector_store,
                            dict(_ADMIN_USER),
                            "test-csrf",
                        )
                    )
            conn.rollback()

            # Rollback sanity: the vault row survived, so its draft still
            # names this input and the bytes must be back at the path.
            vault_rows = conn.execute(
                "SELECT COUNT(*) FROM vaults WHERE id = ?", (vault_id,)
            ).fetchone()[0]
            assert vault_rows == 1

            # The == True shape is load-bearing: the base failure line must
            # read "assert False == True".
            assert storage.exists(relpath) == True  # noqa: E712
        finally:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            self._pool.release_connection(conn)


if __name__ == "__main__":
    unittest.main()
