"""Backend tests for issue #514 Lane B surfaces (upload/library unify).

Covers the four backend deliverables:

- Batched document status endpoint (``GET /documents/status``): per-id
  entries keyed by id, per-id errors for unknown ids, the 100-id cap,
  vault scoping, and wiki/kms compile-job derivations (FU-008).
- Extraction diagnostics: the pure ``build_extraction_diagnostics``
  producer and its exposure on the per-file status payload and list rows
  (PRODUCT-ENH-06) plus the ``partial_embeddings`` status exposure (AC19).
- FolderStore.update_folder transactional move (FOLDER-001): concurrent
  opposing moves cannot create a parent cycle, and error paths leave the
  connection with no open transaction.
- Chat document scope (PRODUCT-ENH-05): ``document_ids`` on both chat
  request models must reach the RAG engine's retrieval seam server-side,
  ANDed with the metadata filter expression (never widening vault access).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy deps so importing app.main is cheap in CI (mirrors the
# repo's existing route-test preamble).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from test_documents_delete_audit import DocumentsDeleteAuditTestBase

# ---------------------------------------------------------------------------
# Batched status endpoint + status payload exposure
# ---------------------------------------------------------------------------


class BatchedDocumentStatusTest(DocumentsDeleteAuditTestBase):
    """GET /documents/status contract (issue #514 FU-008 backend half)."""

    def _get_entries(self, query):
        resp = self.client.get(f"/api/documents/status{query}", headers=self._admin_headers())
        return resp

    def test_batched_status_returns_one_entry_per_requested_id(self):
        fid1 = self._seed_file(file_name="one.txt", parsed_text="alpha")
        fid2 = self._seed_file(file_name="two.txt", parsed_text="beta")
        fid3 = self._seed_file(file_name="three.txt", parsed_text="gamma")

        resp = self._get_entries(f"?ids={fid1},{fid2},{fid3}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        entries = resp.json()["results"]
        self.assertEqual(len(entries), 3, f"one entry per requested id: {entries!r}")
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(set(by_id), {fid1, fid2, fid3})
        for fid in (fid1, fid2, fid3):
            entry = by_id[fid]
            self.assertEqual(entry["status"], "indexed")
            self.assertIs(entry["searchable"], True)
            # Wiki/kms keys are the contract (null values allowed).
            self.assertIn("wiki_status", entry)
            self.assertIn("kms_status", entry)
            self.assertIn("phase", entry)
            self.assertIn("progress_percent", entry)
            self.assertEqual(entry["partial_embeddings"], 0)

    def test_batched_status_unknown_ids_yield_per_id_errors_not_batch_failure(self):
        fid = self._seed_file(file_name="known.txt", parsed_text="known")
        resp = self._get_entries(f"?ids={fid},999999")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()
        by_id = {entry["id"]: entry for entry in body["results"]}
        self.assertIn(fid, by_id)
        self.assertEqual(by_id[fid]["status"], "indexed")
        self.assertIsNotNone(by_id.get(999999))
        self.assertTrue(by_id[999999].get("error"))
        # The error entries are also surfaced under the grouped errors list.
        self.assertEqual(
            [e["id"] for e in body["errors"]], [999999]
        )

    def test_batched_status_rejects_more_than_100_ids(self):
        ids = ",".join(str(i) for i in range(101))
        resp = self._get_entries(f"?ids={ids}")
        self.assertEqual(resp.status_code, 400, resp.text[:300])

    def test_batched_status_rejects_non_integer_tokens(self):
        resp = self._get_entries("?ids=12,not-an-id")
        self.assertEqual(resp.status_code, 400, resp.text[:300])

    def test_batched_status_derives_wiki_and_kms_from_compile_jobs(self):
        fid_with_jobs = self._seed_file(file_name="jobs.txt", parsed_text="jobs")
        fid_pending = self._seed_file(file_name="pending.txt", parsed_text="pending")
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                """INSERT INTO wiki_compile_jobs
                   (vault_id, trigger_type, trigger_id, status)
                   VALUES (?, 'ingest', ?, 'completed')""",
                (2, f"file:{fid_with_jobs}"),
            )
            conn.execute(
                """INSERT INTO wiki_compile_jobs
                   (vault_id, trigger_type, trigger_id, status)
                   VALUES (?, 'ingest', ?, 'failed')""",
                (2, f"file:{fid_with_jobs}"),
            )
            conn.execute(
                """INSERT INTO kms_compile_jobs
                   (vault_id, trigger_type, trigger_id, status)
                   VALUES (?, 'ingest', ?, 'running')""",
                (2, f"file:{fid_with_jobs}"),
            )
            # wiki_pending=1 with no job row -> the "pending" window the
            # per-file route reports.
            conn.execute(
                "UPDATE files SET wiki_pending = 1 WHERE id = ?",
                (fid_pending,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self._get_entries(f"?ids={fid_with_jobs},{fid_pending}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        by_id = {entry["id"]: entry for entry in resp.json()["results"]}
        # Latest wiki job row wins (failed, not completed); kms job exists.
        self.assertEqual(by_id[fid_with_jobs]["wiki_status"], "failed")
        self.assertEqual(by_id[fid_with_jobs]["kms_status"], "running")
        # No job rows + wiki_pending -> pending; no kms job -> null.
        self.assertEqual(by_id[fid_pending]["wiki_status"], "pending")
        self.assertIsNone(by_id[fid_pending]["kms_status"])

    def test_batched_status_vault_scope_reports_out_of_vault_id_as_error(self):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (5, 'Other', 'x')"
            )
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                "VALUES (5, '/uploads/other.txt', 'other.txt', 1, 'indexed')"
            )
            other_fid = cur.lastrowid
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        fid = self._seed_file(file_name="mine.txt", parsed_text="mine")

        resp = self._get_entries(f"?ids={fid},{other_fid}&vault_id=2")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        by_id = {entry["id"]: entry for entry in resp.json()["results"]}
        self.assertEqual(by_id[fid]["status"], "indexed")
        # Out-of-vault id must not resolve through a vault-scoped batch and
        # must not fail the batch.
        self.assertTrue(by_id[other_fid].get("error"))

    def test_batched_status_rejects_empty_id_list(self):
        resp = self._get_entries("?ids=")
        self.assertEqual(resp.status_code, 400, resp.text[:300])
        resp = self._get_entries("?ids=,,,")
        self.assertEqual(resp.status_code, 400, resp.text[:300])

    def test_batched_status_uniform_error_hides_vault_existence(self):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (5, 'Other', 'x')"
            )
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                "VALUES (5, '/uploads/other.txt', 'other.txt', 1, 'indexed')"
            )
            other_fid = cur.lastrowid
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        # No vault_id param: the caller (vault-2 member via _admin_headers)
        # has no read access to vault 5. The per-entry error must use the same
        # uniform "Document not found" wording as an unknown id — a 100-at-a-
        # time existence oracle would defeat the per-file route's 404/403 split.
        resp = self._get_entries(f"?ids={other_fid}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        entry = resp.json()["results"][0]
        self.assertEqual(entry["error"], "Document not found")
        self.assertIsNone(entry["status"])

    def test_batched_status_entries_carry_filename(self):
        fid = self._seed_file(file_name="carried-name.txt", parsed_text="carried")
        resp = self._get_entries(f"?ids={fid}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        results = resp.json()["results"]
        self.assertEqual(results[0]["filename"], "carried-name.txt")

    def test_batched_status_exposes_partial_marker_and_extraction_diagnostics(self):
        fid = self._seed_file(file_name="scanned.txt", parsed_text="text")
        diagnostics = {
            "pages_total": 2,
            "pages_with_text": 1,
            "low_content_pages": [2],
            "ocr_used": True,
            "tables_detected": 1,
            "captions_detected": 0,
            "extraction_version": "extraction-diagnostics-v1",
        }
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE files SET status = 'indexed', chunk_count = 3, "
                "partial_embeddings = 1, extraction_diagnostics = ? WHERE id = ?",
                (json.dumps(diagnostics), fid),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self._get_entries(f"?ids={fid}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        entry = resp.json()["results"][0]
        self.assertEqual(entry["partial_embeddings"], 1)
        self.assertEqual(entry["extraction_diagnostics"], diagnostics)

    def test_legacy_per_file_status_exposes_partial_marker_and_diagnostics(self):
        fid = self._seed_file(file_name="legacy.txt", parsed_text="legacy")
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE files SET chunk_count = 2, partial_embeddings = 1, "
                "extraction_diagnostics = ? WHERE id = ?",
                (json.dumps({"pages_total": 1, "low_content_pages": []}), fid),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get(f"/api/documents/{fid}/status", headers=self._admin_headers())
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        payload = resp.json()
        # Same payload proves retrievable chunks AND reveals parse state.
        self.assertGreater(payload["chunk_count"], 0)
        self.assertEqual(payload["partial_embeddings"], 1)
        self.assertIsInstance(payload["extraction_diagnostics"], dict)
        self.assertEqual(payload["extraction_diagnostics"]["pages_total"], 1)

    def test_list_rows_expose_partial_marker_and_diagnostics(self):
        fid = self._seed_file(file_name="listed.txt", parsed_text="listed")
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE files SET partial_embeddings = 1, extraction_diagnostics = ? "
                "WHERE id = ?",
                (json.dumps({"pages_total": 3, "low_content_pages": [3]}), fid),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get("/api/documents?vault_id=2", headers=self._admin_headers())
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        docs = [d for d in resp.json()["documents"] if d["id"] == fid]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["partial_embeddings"], 1)
        self.assertEqual(docs[0]["extraction_diagnostics"]["pages_total"], 3)
        self.assertEqual(docs[0]["metadata"]["extraction_diagnostics"]["pages_total"], 3)


# ---------------------------------------------------------------------------
# Extraction diagnostics producer
# ---------------------------------------------------------------------------


def _elem(category, text, page):
    return SimpleNamespace(
        category=category,
        text=text,
        metadata=SimpleNamespace(page_number=page),
    )


class BuildExtractionDiagnosticsTest(unittest.TestCase):
    """Pure producer contract (issue #514 PRODUCT-ENH-06 backend half)."""

    def _producer(self):
        from app.services.document_extraction import build_extraction_diagnostics

        return build_extraction_diagnostics

    def _fixture_elements(self):
        # Two-page parse: page 1 carries text + one recovered table; page 2
        # is a scanned image region whose table the parser DROPPED.
        return [
            _elem("Title", "Quarterly figures", 1),
            _elem("NarrativeText", "Revenue grew across all regions.", 1),
            _elem("Table", "Region | Revenue\nEMEA | 12", 1),
            _elem("Image", "", 2),
        ]

    def test_single_positional_call_reveals_low_content_page_and_dropped_table(self):
        diagnostics = self._producer()(self._fixture_elements())
        self.assertEqual(diagnostics["pages_total"], 2)
        self.assertEqual(diagnostics["pages_with_text"], 1)
        self.assertIn(2, diagnostics["low_content_pages"])
        self.assertNotIn(1, diagnostics["low_content_pages"])
        self.assertEqual(diagnostics["tables_detected"], 1)
        self.assertIsInstance(diagnostics["ocr_used"], bool)
        self.assertTrue(diagnostics["extraction_version"])

    def test_keyword_arguments_have_defaults(self):
        diagnostics = self._producer()([])
        self.assertIs(diagnostics["ocr_used"], False)
        self.assertTrue(diagnostics["extraction_version"])

    def test_ocr_flag_is_threaded_not_inferred(self):
        producer = self._producer()
        self.assertIs(producer(self._fixture_elements(), ocr_used=True)["ocr_used"], True)
        self.assertIs(producer(self._fixture_elements(), ocr_used=False)["ocr_used"], False)

    def test_captions_are_counted(self):
        elements = [
            _elem("FigureCaption", "Figure 1: revenue by region", 1),
            _elem("Caption", "Table 1", 2),
        ]
        diagnostics = self._producer()(elements)
        self.assertEqual(diagnostics["captions_detected"], 2)
        self.assertEqual(diagnostics["tables_detected"], 0)

    def test_pageless_parse_is_one_logical_page(self):
        elements = [
            SimpleNamespace(
                category="NarrativeText",
                text="A text document has no page metadata.",
                metadata=SimpleNamespace(page_number=None),
            ),
        ]
        diagnostics = self._producer()(elements)
        self.assertEqual(diagnostics["pages_total"], 1)
        self.assertEqual(diagnostics["pages_with_text"], 1)
        self.assertEqual(diagnostics["low_content_pages"], [])

    def test_empty_parse_reports_zero_pages(self):
        diagnostics = self._producer()([])
        self.assertEqual(diagnostics["pages_total"], 0)
        self.assertEqual(diagnostics["pages_with_text"], 0)
        self.assertEqual(diagnostics["low_content_pages"], [])


# ---------------------------------------------------------------------------
# FolderStore.update_folder transactional move (FOLDER-001)
# ---------------------------------------------------------------------------


class _GatedConnection:
    """sqlite3.Connection proxy that parks the racing thread immediately
    BEFORE its first ``BEGIN IMMEDIATE`` (deterministic interleave)."""

    def __init__(self, conn, gate):
        self._g_conn = conn
        self._g_gate = gate
        self._g_armed = True

    def execute(self, sql, params=()):
        if self._g_armed and sql.strip().upper().startswith("BEGIN"):
            self._g_armed = False
            self._g_gate()
        return self._g_conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._g_conn, name)

    def __setattr__(self, name, value):
        if name.startswith("_g_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._g_conn, name, value)


class FolderTransactionalMoveTest(unittest.TestCase):
    """update_folder must run its checks inside the write transaction."""

    VAULT_ID = 7

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._orig_data_dir = _settings().data_dir
        _settings().data_dir = Path(self._temp_dir)
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _pool, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) "
                "VALUES (?, 'Txn Vault', 'x')",
                (self.VAULT_ID,),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _pool, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()
        _settings().data_dir = self._orig_data_dir
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _connect(self, gate=None):
        import sqlite3

        conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if gate is not None:
            return _GatedConnection(conn, gate)
        return conn

    def test_opposing_concurrent_moves_yield_at_most_one_success(self):
        from app.services.folder_store import FolderStore

        seed_store = FolderStore(self._connect())
        folder_a = seed_store.create_folder(self.VAULT_ID, "A")
        folder_b = seed_store.create_folder(self.VAULT_ID, "B")

        barrier = threading.Barrier(2, timeout=30)
        results = []
        results_lock = threading.Lock()

        def move(conn, folder_id, new_parent_id):
            store = FolderStore(conn)
            try:
                store.update_folder(folder_id, self.VAULT_ID, parent_folder_id=new_parent_id)
                outcome = ("ok", None)
            except Exception as exc:  # noqa: BLE001 - record every failure kind
                outcome = ("err", f"{type(exc).__name__}: {exc}")
            with results_lock:
                results.append(outcome)

        t1 = threading.Thread(
            target=move, args=(self._connect(barrier.wait), folder_a.id, folder_b.id)
        )
        t2 = threading.Thread(
            target=move, args=(self._connect(barrier.wait), folder_b.id, folder_a.id)
        )
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)
        self.assertFalse(t1.is_alive(), "move thread 1 did not finish")
        self.assertFalse(t2.is_alive(), "move thread 2 did not finish")

        successes = [r for r in results if r[0] == "ok"]
        self.assertLessEqual(
            len(successes),
            1,
            f"both opposing moves committed: {results!r}",
        )

        # The stored hierarchy stays acyclic and root-reachable.
        check_conn = self._connect()
        try:
            rows = check_conn.execute(
                "SELECT id, parent_folder_id FROM folders WHERE vault_id = ?",
                (self.VAULT_ID,),
            ).fetchall()
        finally:
            check_conn.close()
        parents = {row[0]: row[1] for row in rows}
        for fid in sorted(parents):
            seen = set()
            cur = fid
            while cur is not None:
                self.assertNotIn(cur, seen, f"parent cycle from folder {fid}: {parents!r}")
                seen.add(cur)
                self.assertIn(cur, parents, f"missing parent {cur}: {parents!r}")
                cur = parents[cur]

    def test_failed_move_releases_transaction_and_lock(self):
        from app.services.folder_store import FolderCycleError, FolderStore

        conn = self._connect()
        store = FolderStore(conn)
        parent = store.create_folder(self.VAULT_ID, "Parent")
        child = store.create_folder(self.VAULT_ID, "Child", parent_folder_id=parent.id)
        with self.assertRaises(FolderCycleError):
            store.update_folder(parent.id, self.VAULT_ID, parent_folder_id=child.id)
        # Rollback must leave the pooled connection with no open transaction
        # (an open BEGIN IMMEDIATE would hold the write lock).
        self.assertFalse(conn.in_transaction, "connection left inside a transaction")
        # And the hierarchy is unchanged.
        self.assertIsNone(store.get_folder(parent.id, self.VAULT_ID).parent_folder_id)
        conn.close()

    def test_not_found_update_releases_transaction(self):
        from app.services.folder_store import FolderStore

        conn = self._connect()
        store = FolderStore(conn)
        self.assertIsNone(store.update_folder(999999, self.VAULT_ID, name="x"))
        self.assertFalse(conn.in_transaction, "connection left inside a transaction")
        conn.close()


def _settings():
    from app.config import settings

    return settings


# ---------------------------------------------------------------------------
# Chat document scope (PRODUCT-ENH-05)
# ---------------------------------------------------------------------------


class ChatDocumentScopeRouteTest(unittest.TestCase):
    """document_ids on ChatRequest/ChatStreamRequest must reach the engine."""

    SCOPE_DOC_ID = 42

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.main import app

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._originals = {
            "jwt_secret_key": _settings().jwt_secret_key,
            "users_enabled": _settings().users_enabled,
            "data_dir": _settings().data_dir,
        }
        _settings().data_dir = Path(self._temp_dir)
        _settings().jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        _settings().users_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _pool, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        from _db_pool import SimpleConnectionPool

        self._connection_pool = SimpleConnectionPool(self._db_path)

        from app.api.deps import get_db, get_rag_engine, get_vector_store

        self._deps = (get_db, get_rag_engine, get_vector_store)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        self.engine_calls = []

        async def recording_query(*args, **kwargs):
            self.engine_calls.append(dict(kwargs))
            yield {"type": "content", "content": "scoped answer"}
            yield {
                "type": "done",
                "sources": [{"file_id": str(self.SCOPE_DOC_ID), "filename": "scoped.txt"}],
                "memories_used": [],
            }

        self._mock_vector_store = MagicMock()
        self._mock_vector_store._ready = True
        self._mock_rag_engine = MagicMock()
        self._mock_rag_engine.llm_client = None
        self._mock_rag_engine.query = recording_query

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_rag_engine] = lambda: self._mock_rag_engine

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "unused-test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1, 'superadmin', ?, 'Super Admin', 'superadmin', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (3, 'member1', ?, 'Member One', 'member', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2, 'Scoped Vault', 's')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (2, 3, 'read', 1)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, file_size, status) "
                "VALUES (?, 2, '/uploads/scoped.txt', 'scoped.txt', 10, 'indexed')",
                (self.SCOPE_DOC_ID,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.main import app
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _pool, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        for dep in self._deps:
            app.dependency_overrides.pop(dep, None)
        from app.api.routes.chat import get_stream_auth

        app.dependency_overrides.pop(get_stream_auth, None)

        _settings().jwt_secret_key = self._originals["jwt_secret_key"]
        _settings().users_enabled = self._originals["users_enabled"]
        _settings().data_dir = self._originals["data_dir"]
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _member_headers(self):
        from app.services.auth_service import (
            compute_client_fingerprint,
            create_access_token,
        )

        return {
            "Authorization": (
                "Bearer "
                + create_access_token(
                    3, "member1", "member", client_fingerprint=compute_client_fingerprint("")
                )
            )
        }

    def test_chat_request_document_scope_reaches_engine(self):
        resp = self.client.post(
            "/api/chat",
            json={
                "message": "Summarize this document",
                "vault_id": 2,
                "document_ids": [self.SCOPE_DOC_ID],
            },
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertTrue(self.engine_calls, "the RAG engine must have been queried")
        scoped = [kw["document_ids"] for kw in self.engine_calls if "document_ids" in kw]
        self.assertTrue(
            scoped and self.SCOPE_DOC_ID in scoped[0],
            f"document scope never reached retrieval; kwargs={self.engine_calls!r}",
        )

    def test_stream_request_document_scope_reaches_engine(self):
        from app.api.routes.chat import get_stream_auth
        from app.main import app

        app.dependency_overrides[get_stream_auth] = lambda: {
            "id": "3",
            "username": "member1",
            "role": "member",
        }
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "Summarize this document"}],
                "vault_id": 2,
                "document_ids": [self.SCOPE_DOC_ID],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertTrue(self.engine_calls, "the RAG engine must have been queried")
        scoped = [kw["document_ids"] for kw in self.engine_calls if "document_ids" in kw]
        self.assertTrue(
            scoped and self.SCOPE_DOC_ID in scoped[0],
            f"document scope never reached retrieval; kwargs={self.engine_calls!r}",
        )

    def test_chat_document_ids_capped_at_100(self):
        import pytest
        from pydantic import ValidationError

        from app.api.routes.chat import ChatRequest, ChatStreamRequest

        with pytest.raises(ValidationError):
            ChatRequest(message="q", document_ids=list(range(101)))
        ChatRequest(message="q", document_ids=list(range(100)))
        with pytest.raises(ValidationError):
            ChatStreamRequest(
                messages=[{"role": "user", "content": "q"}],
                document_ids=list(range(101)),
            )
        ChatStreamRequest(
            messages=[{"role": "user", "content": "q"}],
            document_ids=list(range(100)),
        )


class DocumentScopeFilterExprTest(unittest.TestCase):
    """The scope expression shape used at the retrieval seam."""

    def test_scope_expr_quotes_string_file_ids(self):
        from app.services.rag_engine import _document_scope_filter_expr

        self.assertEqual(
            _document_scope_filter_expr([42, 43]),
            "file_id IN ('42', '43')",
        )


class ChatDocumentScopeEngineTest(unittest.TestCase):
    """query(document_ids=...) must restrict the REAL retrieval seam.

    Drives the standard pipeline with a recording vector store and asserts
    every search call carries the document scope expression (ANDed with the
    metadata-filter expression when both are supplied).
    """

    def _run_query(self, engine_hook=None, **query_kwargs):
        from app.services.rag_engine import RAGEngine

        class _Store:
            def __init__(self):
                self.filter_exprs = []

            async def search(self, embedding, limit, vault_id=None, query_text="",
                             hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
                self.filter_exprs.append(filter_expr)
                return [{
                    "id": "42_0", "file_id": "42",
                    "text": "Paris is the capital of France.",
                    "_distance": 0.1, "metadata": {},
                }]

            def get_fts_exceptions(self):
                return 0

        class _Embed:
            async def embed_single(self, text):
                return [0.1, 0.2, 0.3]

            async def embed_passage(self, text):
                return [0.1, 0.2, 0.3]

        class _Memory:
            def detect_memory_intent(self, text):
                return None

        class _LLM:
            base_url = "stub"
            model = "stub"

            def __init__(self):
                self.last_metrics = {}

            async def chat_completion(self, messages, **kw):
                return "The capital of France is Paris, a famous city."

            async def chat_completion_stream(self, messages, **kw):
                yield "The capital of France is Paris, a famous city."

        store = _Store()
        engine = RAGEngine(
            embedding_service=_Embed(),
            vector_store=store,
            memory_store=_Memory(),
            llm_client=_LLM(),
            reranking_service=None,
        )
        if engine_hook is not None:
            engine_hook(engine)

        with unittest.mock.patch("app.services.rag_engine.settings") as mock_settings, patch_pool_unavailable():
            mock_settings.agentic_rag_enabled = False
            mock_settings.default_chat_mode = "thinking"
            mock_settings.query_transformation_enabled = False
            mock_settings.memory_retrieval_enabled = False
            mock_settings.retrieval_evaluation_enabled = False
            mock_settings.context_distillation_enabled = False
            mock_settings.context_distillation_synthesis_enabled = False
            mock_settings.context_max_tokens = 0
            mock_settings.parent_retrieval_enabled = False
            mock_settings.retrieval_recency_weight = 0.0
            mock_settings.rrf_legacy_mode = False
            mock_settings.exact_match_promote = False
            mock_settings.reranking_enabled = False
            mock_settings.hybrid_search_enabled = False
            mock_settings.hybrid_alpha = 0.6
            mock_settings.maintenance_mode = False
            mock_settings.max_distance_threshold = 1.0
            mock_settings.rag_relevance_threshold = 0.5
            mock_settings.retrieval_top_k = 10
            mock_settings.retrieval_window = 0
            mock_settings.initial_retrieval_top_k = 10
            mock_settings.reranker_top_n = 5
            mock_settings.thinking_max_tokens = 1024
            mock_settings.rag_trace_in_response = False
            mock_settings.kms_enabled = False

            done = None
            messages = []

            async def _collect():
                nonlocal done
                async for chunk in engine.query(
                    "what is the capital", [], stream=False, vault_id=1, **query_kwargs
                ):
                    messages.append(chunk)
                    if chunk.get("type") == "done":
                        done = chunk

            asyncio.run(_collect())

        self.assertIsNotNone(done, f"query never emitted done; messages={messages!r}")
        self.assertTrue(store.filter_exprs, "vector_store.search was never called")
        return store.filter_exprs

    def test_document_scope_reaches_every_search_call(self):
        exprs = self._run_query(document_ids=[42])
        self.assertTrue(exprs)
        for expr in exprs:
            self.assertIsNotNone(expr, "scope must never be silently dropped")
            self.assertIn("file_id IN ('42')", expr)

    def test_document_scope_ands_with_metadata_filter(self):
        exprs = self._run_query(
            document_ids=[42], metadata_filter={"author": "alice@corp.example"}
        )
        self.assertTrue(exprs)
        for expr in exprs:
            # The metadata resolution (pool unavailable in this test) yields
            # its zero-match sentinel; the document scope must be ANDed onto
            # it, not replace it.
            self.assertIn("file_id IN ('')", expr)
            self.assertIn("file_id IN ('42')", expr)
            self.assertIn(" AND ", expr)

    def test_empty_scope_means_no_scope_requested(self):
        # F-004: document_ids=[] is falsy and takes the no-scope path —
        # whole-vault retrieval, no file_id IN expression anywhere.
        exprs = self._run_query(document_ids=[])
        self.assertTrue(exprs)
        for expr in exprs:
            self.assertIsNone(expr)

    def test_scoped_partial_document_survives_visibility_filter(self):
        from app.services.document_retrieval import DocumentRetrievalService, RAGSource

        recorded = []

        class _RecordingRetrieval:
            # Mirrors the real DocumentRetrievalService contract: raw dict
            # records in, RAGSource objects out (the pipeline after the seam
            # reads attributes like .file_id, not dict keys), and
            # to_source_metadata for the done payload's source list.
            _serializer = DocumentRetrievalService()

            async def filter_relevant(self, results, reranked=False, indexed_file_ids=None):
                recorded.append(indexed_file_ids)
                return [
                    RAGSource(
                        text=record.get("text", ""),
                        file_id=record.get("file_id", ""),
                        score=float(record.get("_distance", 0.0) or 0.0),
                        metadata=dict(record.get("metadata") or {}),
                    )
                    for record in results
                ]

            def to_source_metadata(self, chunk, source_index=0):
                return self._serializer.to_source_metadata(chunk, source_index=source_index)

        def _hook(engine):
            # SQLite says only file 999 is fully indexed; file 42 is partial.
            engine._get_indexed_file_ids = lambda vault_id: {"999"}
            # The scoped-partial seam admits the explicitly named partial id.
            engine._get_scoped_partial_file_ids = lambda ids, vault_id: {"42"}
            engine.document_retrieval = _RecordingRetrieval()

        self._run_query(document_ids=[42], engine_hook=_hook)
        self.assertTrue(recorded)
        for indexed in recorded:
            self.assertIsNotNone(indexed)
            self.assertIn("42", indexed, "scoped partial id must join the visibility set")
            self.assertIn("999", indexed)


class ScopedPartialFileIdsTest(DocumentsDeleteAuditTestBase):
    """Real-DB contract of the scoped-partial admission seam (PRR-002 fix).

    ``_get_scoped_partial_file_ids`` admits only in-vault ids whose file row
    has status='partial', and never leaks into unscoped visibility
    (``_get_indexed_file_ids`` keeps hiding partial files, Issue #13).
    """

    def _seed_file_status(self, vault_id, file_name, status):
        # Same INSERT pattern as _seed_file, with the status overridden.
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status, parsed_text) "
                "VALUES (?,?,?,?,?,?)",
                (vault_id, f"/uploads/{file_name}", file_name, 1, status, "seed"),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            self._connection_pool.release_connection(conn)

    def _make_engine(self):
        # Minimal engine construction the same way ChatDocumentScopeEngineTest
        # does (stub _Store/_Embed/_Memory/_LLM) — the pool-backed lookups read
        # this fixture's real SQLite via settings.sqlite_path.
        from app.services.rag_engine import RAGEngine

        class _Store:
            async def search(self, embedding, limit, vault_id=None, query_text="",
                             hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
                return []

            def get_fts_exceptions(self):
                return 0

        class _Embed:
            async def embed_single(self, text):
                return [0.1, 0.2, 0.3]

            async def embed_passage(self, text):
                return [0.1, 0.2, 0.3]

        class _Memory:
            def detect_memory_intent(self, text):
                return None

        class _LLM:
            base_url = "stub"
            model = "stub"

            def __init__(self):
                self.last_metrics = {}

            async def chat_completion(self, messages, **kw):
                return "stub"

            async def chat_completion_stream(self, messages, **kw):
                yield "stub"

        return RAGEngine(
            embedding_service=_Embed(),
            vector_store=_Store(),
            memory_store=_Memory(),
            llm_client=_LLM(),
            reranking_service=None,
        )

    def test_scoped_partial_file_ids_resolve_against_real_db(self):
        partial_fid = self._seed_file_status(2, "partial.txt", "partial")
        other_fid = self._seed_file(file_name="done.txt", parsed_text="done")
        engine = self._make_engine()

        self.assertEqual(
            engine._get_scoped_partial_file_ids([partial_fid], 2),
            {str(partial_fid)},
        )
        # Vault mismatch: an out-of-vault id is never admitted.
        self.assertEqual(engine._get_scoped_partial_file_ids([partial_fid], 5), set())
        # Not partial: a fully indexed id is not a scoped-partial id.
        self.assertEqual(engine._get_scoped_partial_file_ids([other_fid], 2), set())
        # Unscoped visibility is unchanged: partial files stay hidden from
        # _get_indexed_file_ids (Issue #13), while the indexed one shows up.
        indexed = engine._get_indexed_file_ids(2)
        self.assertNotIn(str(partial_fid), indexed)
        self.assertIn(str(other_fid), indexed)
        # Empty scope: nothing to admit.
        self.assertEqual(engine._get_scoped_partial_file_ids([], 2), set())


class _patch_pool_unavailable:
    """Context manager forcing pool lookups to fail (zero-match semantics)."""

    def __enter__(self):
        self._patcher = unittest.mock.patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        )
        # metadata_filter resolution imports get_pool from
        # app.models.database directly, so patch that too.
        self._patcher2 = unittest.mock.patch(
            "app.models.database.get_pool",
            side_effect=RuntimeError("no db in test"),
        )
        self._patcher.start()
        self._patcher2.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        self._patcher2.stop()
        return False


def patch_pool_unavailable():
    return _patch_pool_unavailable()


if __name__ == "__main__":
    unittest.main()
