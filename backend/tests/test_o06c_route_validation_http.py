"""HTTP-level validation pins for issue #796 / PR #972 (feedback round).

The frozen checks and the o06b battery validate the pydantic models
directly; this file pins the same contracts at the HTTP layer (422 per new
Literal surface, 400s on the bulk route's three guarded shapes, the KMS
500 reclassification end-to-end), which the review identified as the
repo-convention gap (100+ `status_code == 422` assertions elsewhere, zero
in the two new files).
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_current_active_user, get_db, get_vector_store  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.security import csrf_protect  # noqa: E402

_MOCK_SUPERADMIN = {
    "id": 0,
    "username": "admin",
    "full_name": "Admin",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


class _SimplePool:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._pool: Queue = Queue(maxsize=5)
        self._closed = False

    def get(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            return conn

    def release(self, conn: sqlite3.Connection) -> None:
        if not self._closed:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                conn.close()

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except Empty:
                break


class HttpValidationBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self._temp_dir)
        db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()

        from app.models.database import init_db
        init_db(db_path)

        self._pool = _SimplePool(db_path)
        conn = self._pool.get()
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Default')"
        )
        conn.commit()
        self._pool.release(conn)

        def _get_db_override():
            conn = self._pool.get()
            try:
                yield conn
            finally:
                self._pool.release(conn)

        app.dependency_overrides[get_current_active_user] = (
            lambda: _MOCK_SUPERADMIN
        )
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[get_vector_store] = lambda: MagicMock()
        app.dependency_overrides[csrf_protect] = lambda: "test-token"

    def tearDown(self):
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(csrf_protect, None)
        self._pool.close_all()
        settings.data_dir = self._original_data_dir
        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _create_page(self, **overrides):
        payload = {
            "vault_id": 1,
            "title": "Pin Page",
            "page_type": "entity",
        }
        payload.update(overrides)
        resp = self.client.post("/api/wiki/pages", json=payload)
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _create_claim(self, **overrides):
        payload = {
            "vault_id": 1,
            "claim_text": "Pin claim text",
            "source_type": "manual",
        }
        payload.update(overrides)
        resp = self.client.post("/api/wiki/claims", json=payload)
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()


class TestWikiLiteralSurfacesHttp(HttpValidationBase):
    def test_page_create_oov_page_type_422(self):
        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "t", "page_type": "bogus"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_page_create_oov_status_422(self):
        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "t", "page_type": "entity",
                  "status": "bogus"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_page_create_confidence_out_of_range_422(self):
        for value in (7.5, -3):
            resp = self.client.post(
                "/api/wiki/pages",
                json={"vault_id": 1, "title": "t", "page_type": "entity",
                      "confidence": value},
            )
            self.assertEqual(resp.status_code, 422)

    def test_page_update_oov_and_bounds_422(self):
        page = self._create_page()
        for payload in (
            {"status": "bogus"},
            {"page_type": "bogus"},
            {"confidence": 7.5},
            {"title": ""},  # min_length=1 (feedback round M-2 pin)
            {"title": "x" * 501},
        ):
            resp = self.client.put(
                f"/api/wiki/pages/{page['id']}", json=payload
            )
            self.assertEqual(resp.status_code, 422, payload)

    def test_claim_create_oov_422(self):
        for payload in (
            {"vault_id": 1, "claim_text": "c", "source_type": "bogus"},
            {"vault_id": 1, "claim_text": "c", "source_type": "manual",
             "status": "bogus"},
            {"vault_id": 1, "claim_text": "c", "source_type": "manual",
             "confidence": 7.5},
        ):
            resp = self.client.post("/api/wiki/claims", json=payload)
            self.assertEqual(resp.status_code, 422, payload)

    def test_claim_update_oov_and_bounds_422(self):
        claim = self._create_claim()
        for payload in (
            {"status": "bogus"},
            {"source_type": "bogus"},
            {"confidence": -3},
            {"claim_text": ""},  # min_length=1 (feedback round M-2 pin)
            {"claim_text": "x" * 2001},
        ):
            resp = self.client.put(
                f"/api/wiki/claims/{claim['id']}", json=payload
            )
            self.assertEqual(resp.status_code, 422, payload)


class TestBulkRouteHttp(HttpValidationBase):
    def _page_ids(self, n=2):
        return [self._create_page(title=f"Bulk {i}")["id"] for i in range(n)]

    def test_bulk_oov_status_422_at_route(self):
        ids = self._page_ids()
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "update",
                  "updates": {"status": "bogus"}},
        )
        self.assertEqual(resp.status_code, 422)

    def test_bulk_over_long_title_422_at_route(self):
        ids = self._page_ids()
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "update",
                  "updates": {"title": "x" * 501}},
        )
        self.assertEqual(resp.status_code, 422)

    def test_bulk_absent_updates_400(self):
        ids = self._page_ids()
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "update"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_bulk_empty_updates_400(self):
        ids = self._page_ids()
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "update",
                  "updates": {}},
        )
        self.assertEqual(resp.status_code, 400)

    def test_bulk_all_fields_null_400(self):
        # Second guard: every explicitly-set field null on a NOT NULL
        # column drops out, leaving nothing to apply.
        ids = self._page_ids()
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "update",
                  "updates": {"title": None}},
        )
        self.assertEqual(resp.status_code, 400)

    def test_bulk_expected_version_silently_excluded(self):
        # M-4 pin: expected_version is deliberately excluded from the typed
        # bulk model (per-page OCC has no bulk meaning). A stale value is
        # silently ignored -- the update APPLIES (documented contract).
        ids = self._page_ids(1)
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "update",
                  "updates": {"expected_version": 999, "status": "verified"}},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["updated"], 1)
        detail = self.client.get(f"/api/wiki/pages/{ids[0]}")
        self.assertEqual(detail.json()["status"], "verified")


class TestKmsCreateHttp(HttpValidationBase):
    def test_internal_failure_is_500_with_generic_detail(self):
        import app.api.routes.kms as kms_mod

        with patch.object(
            kms_mod.KMSStore,
            "create_entry",
            side_effect=RuntimeError("O06-SENTINEL internal failure"),
        ):
            resp = self.client.post(
                "/api/kms/entries",
                json={"vault_id": 1, "title": "t", "status": "draft"},
            )
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("O06-SENTINEL", resp.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
