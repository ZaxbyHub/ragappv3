"""Regression specs for issue #1119 AC2: the ``409 promotion_path_conflict``
response must not leak the server filesystem destination path into the
client-visible detail, while the server-side log keeps the full destination
path for operators.

The service raises ``DraftPromotionPathConflictError`` with the destination
path embedded in the exception message and the Draft Room route renders
``str(exc)`` verbatim as the HTTP detail, so the collision 409 currently
discloses the server-side uploads directory. The frozen check drives the REAL
promote route against a REAL sqlite database with a pre-existing ``files``
row at the reserved destination (the same mechanic as
``test_draft_promote.py``'s ``TestPromoteNeverAdoptsAnExistingFileRow``) and
asserts:

  1. the response is ``409`` with code ``promotion_path_conflict``,
  2. the client-visible ``detail`` contains no rendering of the server
     destination path (raw, backslash-escaped/repr form, or forward-slash
     form — the current leak renders through ``{path!r}``, which doubles the
     backslashes) and no rendering of its parent directory,
  3. some server-side log record produced by the request carries the full
     destination path (any logger, any level — captured through a DEBUG-level
     root handler so the assertion does not prescribe where the fix logs it).

Harness duplicated from ``test_draft_promote.py``'s ``DraftRoomTestBase`` /
``DraftPromoteTestBase`` (per this package's convention of duplicating rather
than importing across test files).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from fastapi.testclient import TestClient

from app.api.deps import get_background_processor, get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import CSRFManager, csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token


class _PoolWithConnectionCM:
    """Thread-safe SQLite pool exposing the ``get_connection``/
    ``release_connection`` idiom (backs the ``get_db`` override) and the
    ``with pool.connection() as conn`` context manager (mirrors the production
    ``SQLiteConnectionPool`` surfaces). Copied from test_draft_promote.py."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._pool: Queue = Queue(maxsize=5)
        self._closed = False

    def get_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    async def get_connection_async(
        self, max_wait_attempts: int = 3
    ) -> sqlite3.Connection:
        return await asyncio.to_thread(self.get_connection)

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        if self._closed:
            conn.close()
            return
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

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)

    @asynccontextmanager
    async def connection_async(self, max_wait_attempts: int = 3):
        conn = await self.get_connection_async(max_wait_attempts)
        try:
            yield conn
        finally:
            self.release_connection(conn)


def _default_brief(**overrides) -> dict:
    brief = {
        "piece_type": "article",
        "audience": "general readers",
        "purpose": "inform readers about the topic",
        "tone": "clear and direct",
        "target_words": 500,
        "transformation_strength": "moderate",
        "primary_input_id": None,
        "must_include": [],
        "must_avoid": [],
        "preserve_quotes": True,
        "preserve_numbers": True,
        "preserve_uncertainty": True,
        "drafting_priority": "balanced",
        "additional_instructions": "",
    }
    brief.update(overrides)
    return brief


class _RootCapture(logging.Handler):
    """Capture every propagated log record (message plus any exception text)
    at DEBUG level; level- and logger-agnostic so the spec does not prescribe
    where the fix logs the destination path."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.entries: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.entries.append(self.format(record))
        except Exception:  # pragma: no cover - never fail the request under test
            pass


class DraftRoomTestBase(unittest.TestCase):
    OWNER_ID = 1
    OTHER_ID = 2
    READ_VAULT_ID = 2
    NO_ACCESS_VAULT_ID = 9

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir
        self._original_draft_room_enabled = settings.draft_room_enabled

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True
        settings.draft_room_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = _PoolWithConnectionCM(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        app.state.db_pool = self._connection_pool
        app.state.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = MagicMock()
        self._mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
        self._mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        self._mock_background_processor = MagicMock()
        self._mock_background_processor.enqueue = AsyncMock(return_value=None)
        app.dependency_overrides[get_background_processor] = lambda: (
            self._mock_background_processor
        )

        self._prev_bp = getattr(app.state, "background_processor", None)
        self._had_bp = hasattr(app.state, "background_processor")
        app.state.background_processor = self._mock_background_processor

        self._prev_djp = getattr(app.state, "draft_job_processor", None)
        self._had_djp = hasattr(app.state, "draft_job_processor")
        app.state.draft_job_processor = object()
        self.addCleanup(self._restore_worker_stubs)

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1,'owner',?, 'Owner','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (2,'other',?, 'Other','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Read Vault','r')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (2,1,'write',1)"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _restore_worker_stubs(self) -> None:
        if self._had_djp:
            app.state.draft_job_processor = self._prev_djp
        elif hasattr(app.state, "draft_job_processor"):
            del app.state.draft_job_processor
        if getattr(self, "_had_bp", False):
            app.state.background_processor = self._prev_bp
        elif hasattr(app.state, "background_processor") and hasattr(self, "_prev_bp"):
            del app.state.background_processor

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        settings.draft_room_enabled = self._original_draft_room_enabled
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_background_processor, None)
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
        if hasattr(app.state, "csrf_manager"):
            del app.state.csrf_manager
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _headers(self, user_id=OWNER_ID, username="owner", role="member"):
        return {
            "Authorization": f"Bearer {create_access_token(user_id, username, role, client_fingerprint=compute_client_fingerprint(''))}"
        }

    def _owner_headers(self):
        return self._headers(self.OWNER_ID, "owner")

    def _create_draft(
        self,
        *,
        headers=None,
        vault_id=READ_VAULT_ID,
        mode="rewrite",
        title="My Draft",
        brief=None,
    ):
        headers = headers or self._owner_headers()
        resp = self.client.post(
            "/api/draft-room/drafts",
            json={
                "vault_id": vault_id,
                "title": title,
                "mode": mode,
                "tier": "standard",
                "brief": brief or _default_brief(),
            },
            headers=headers,
        )
        return resp

    def _upload_input(
        self,
        draft_id,
        *,
        headers=None,
        filename="manuscript.txt",
        content=b"Hello world. This is the manuscript body.",
        role="manuscript",
        authority="primary",
        content_type="text/plain",
    ):
        headers = headers or self._owner_headers()
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/inputs",
            data={"role": role, "authority": authority},
            files={"file": (filename, content, content_type)},
            headers=headers,
        )
        return resp


class DraftPromoteTestBase(DraftRoomTestBase):
    """Extra helpers shared by promotion tests (copied from
    test_draft_promote.py, trimmed to the legs this spec drives)."""

    def _mark_input_ready(self, input_id: int, *, parse_status: str = "ready") -> None:
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_inputs SET parse_status = ?, parsed_text = 'parsed body', "
                "parsed_char_count = 11 WHERE id = ?",
                (parse_status, input_id),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def _promote(
        self,
        draft_id,
        *,
        source_type="input",
        source_id,
        title="Promoted Document",
        headers=None,
    ):
        headers = headers or self._owner_headers()
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/promote",
            json={"source_type": source_type, "source_id": source_id, "title": title},
            headers=headers,
        )

    def _seed_victim_row(self, dest_path, expected_filename):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, file_type, source, status) "
                "VALUES (?, ?, ?, 'OLD_HASH_AAA', 12345, '.txt', 'upload', 'indexed')",
                (self.READ_VAULT_ID, str(dest_path), expected_filename),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)


def _path_leak_variants(path: Path) -> list[str]:
    """Every rendering of a server filesystem path that must not appear in a
    client-visible detail: the raw OS form, the backslash-doubled form (the
    current leak renders the path through ``{path!r}``, which escapes each
    backslash), and the forward-slash form, for both the full destination and
    its parent directory (the uploads dir is itself a server path)."""
    variants: list[str] = []
    raw = str(path)
    for candidate in (
        raw,
        raw.replace("\\", "\\\\"),
        path.as_posix(),
        str(path.parent),
        str(path.parent).replace("\\", "\\\\"),
        path.parent.as_posix(),
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


class TestPromotionConflictDetailHygiene(DraftPromoteTestBase):
    def test_conflict_detail_hides_server_path_and_log_keeps_it(self):
        """Issue #1119 AC2: a promotion whose destination path collides with
        an existing ``files`` row answers ``409 promotion_path_conflict`` with
        a client-visible detail free of the server filesystem path, while the
        server-side log keeps the full destination path."""
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        title = "Existing Document"
        expected_filename = "Existing_Document.txt"
        dest_path = settings.vault_uploads_dir(self.READ_VAULT_ID) / expected_filename
        self._seed_victim_row(dest_path, expected_filename)
        self.assertFalse(dest_path.exists(), "victim bytes are missing (precondition)")

        capture = _RootCapture()
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        try:
            resp = self._promote(
                draft_id, source_type="input", source_id=input_id, title=title
            )
        finally:
            root.removeHandler(capture)
            root.setLevel(previous_level)

        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertEqual(body["code"], "promotion_path_conflict", body)

        detail = str(body.get("detail", ""))
        for variant in _path_leak_variants(dest_path):
            self.assertNotIn(
                variant,
                detail,
                "client-visible promotion_path_conflict detail leaks the server "
                f"filesystem destination path ({variant!r}): {detail!r}",
            )

        log_text = "\n".join(capture.entries)
        self.assertIn(
            str(dest_path),
            log_text,
            "server-side log lines do not carry the full destination path for a "
            "promotion_path_conflict rejection",
        )
