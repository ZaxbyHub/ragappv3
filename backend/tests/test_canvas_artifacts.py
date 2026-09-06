"""Versioned canvas artifact tests (issue #509).

Exercises the full TestClient -> FastAPI -> CanvasStore -> SQLite boundary
against a real per-test database, covering the 18 named edge cases from the
#509 fix plan (plus the edit-range concurrent-save discard case, the
capabilities endpoint, and router registration smoke). Model-backed
edit-range calls use a deterministic AsyncMock installed on
``app.state.llm_client`` so no external provider is contacted.

Note: this module deliberately does not manage request-forgery tokens itself;
the shared conftest toggles the test-only bypass for modules like this one
that do not assert that protection.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same guards as the other route suites).
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

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.config import settings
from app.main import app
from app.models.database import _pool_cache, _pool_cache_lock, init_db, run_migrations
from app.services.auth_service import compute_client_fingerprint, create_access_token
from app.services.canvas_store import CanvasStore
from app.services.llm_client import LLMError

PW = "unused-test-password-hash"


class CanvasTestBase(unittest.TestCase):
    """Shared fixture chain: temp DB, app client, users/vault/session seeds."""

    def setUp(self):
        self.client = TestClient(app)
        # Match the fingerprint baked into generated tokens.
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._originals = {
            "jwt_secret_key": settings.jwt_secret_key,
            "users_enabled": settings.users_enabled,
            "data_dir": settings.data_dir,
            "canvas_enabled": settings.canvas_enabled,
            "canvas_max_artifact_kb": settings.canvas_max_artifact_kb,
        }
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.canvas_enabled = True
        settings.canvas_max_artifact_kb = 512

        self._db_path = str(Path(self._temp_dir) / "app.db")

        self._reset_shared_pool_cache()
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        # Seed users AFTER migrations so the orphan->Default-vault assignment
        # does not grant the no-access member a read row on vault 1.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM canvas_versions")
            conn.execute("DELETE FROM canvas_artifacts")
            conn.execute("DELETE FROM chat_messages")
            conn.execute("DELETE FROM chat_sessions")
            conn.execute("DELETE FROM users WHERE id != 0")
            # 1: superadmin (policy passes everything)
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", PW, "Super Admin", "superadmin"),
            )
            # 2: member with READ on vault 1 (Default)
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (2, "member1", PW, "Member One", "member"),
            )
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (1, 2, 'read', 1)"
            )
            # 3: member with no vault access at all
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (3, "member2", PW, "Member Two", "member"),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        self._reset_shared_pool_cache()
        app.dependency_overrides.pop(get_db, None)
        if hasattr(app.state, "llm_client"):
            delattr(app.state, "llm_client")
        for key, value in self._originals.items():
            setattr(settings, key, value)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    @staticmethod
    def _reset_shared_pool_cache():
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()

    # ── seeds ────────────────────────────────────────────────────────────

    def _db(self):
        return self._connection_pool.get_connection()

    def _release(self, conn):
        self._connection_pool.release_connection(conn)

    def _make_session(self, vault_id=1, user_id=1, title="Canvas session"):
        conn = self._db()
        try:
            session_id = conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
                (vault_id, user_id, title),
            ).lastrowid
            conn.commit()
            return session_id
        finally:
            self._release(conn)

    def _make_message(self, session_id, role="assistant", content="Answer", turn_id=None):
        conn = self._db()
        try:
            message_id = conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, sources, turn_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, None, turn_id),
            ).lastrowid
            conn.commit()
            return message_id
        finally:
            self._release(conn)

    # ── auth ─────────────────────────────────────────────────────────────

    def _token(self, user_id, username, role):
        return create_access_token(
            user_id, username, role, client_fingerprint=compute_client_fingerprint("")
        )

    def _super_headers(self):
        return {"Authorization": f"Bearer {self._token(1, 'superadmin', 'superadmin')}"}

    def _member_headers(self):
        return {"Authorization": f"Bearer {self._token(2, 'member1', 'member')}"}

    def _no_access_headers(self):
        return {"Authorization": f"Bearer {self._token(3, 'member2', 'member')}"}

    # ── canvas helpers ───────────────────────────────────────────────────

    def _create_artifact(self, session_id, content="print('hello')\n", name="solution",
                         kind="code", language="python", message_id=None, turn_id=None,
                         source_refs=None, headers=None):
        payload = {"kind": kind, "name": name, "content": content}
        if language is not None:
            payload["language"] = language
        if message_id is not None:
            payload["message_id"] = message_id
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if source_refs is not None:
            payload["source_refs"] = source_refs
        return self.client.post(
            f"/api/chat/sessions/{session_id}/artifacts",
            json=payload,
            headers=headers or self._super_headers(),
        )

    def _install_llm(self, replacement):
        mock = MagicMock()
        mock.chat_completion = AsyncMock(return_value=replacement)
        app.state.llm_client = mock
        return mock


# ---------------------------------------------------------------------------
# Happy paths + router smoke
# ---------------------------------------------------------------------------


class TestCanvasCreateAndDetail(CanvasTestBase):
    def test_create_and_get_artifact_roundtrip(self):
        session_id = self._make_session()
        response = self._create_artifact(session_id)
        self.assertEqual(response.status_code, 200, response.text)
        artifact = response.json()
        self.assertTrue(artifact["artifact_uid"].startswith("cav_"))
        self.assertEqual(artifact["kind"], "code")
        self.assertEqual(artifact["current_version_no"], 1)
        self.assertEqual(artifact["current_version"]["origin"], "created")
        self.assertEqual(artifact["current_version"]["content"], "print('hello')\n")

        detail = self.client.get(
            f"/api/canvas/artifacts/{artifact['artifact_uid']}",
            headers=self._super_headers(),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["artifact_uid"], artifact["artifact_uid"])
        self.assertEqual(
            detail.json()["current_version"]["content"], "print('hello')\n"
        )

        version = self.client.get(
            f"/api/canvas/artifacts/{artifact['artifact_uid']}/versions/1",
            headers=self._super_headers(),
        )
        self.assertEqual(version.status_code, 200)
        self.assertEqual(version.json()["origin"], "created")
        self.assertEqual(version.json()["content"], "print('hello')\n")

    def test_capabilities_endpoint(self):
        response = self.client.get("/api/canvas/capabilities", headers=self._super_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": True})

    def test_two_artifacts_same_name(self):
        """Same-name artifacts keep independent identities (uid is identity)."""
        session_id = self._make_session()
        first = self._create_artifact(session_id, content="a = 1\n", name="solution")
        second = self._create_artifact(session_id, content="a = 2\n", name="solution")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        uid_a = first.json()["artifact_uid"]
        uid_b = second.json()["artifact_uid"]
        self.assertNotEqual(uid_a, uid_b)

        listing = self.client.get(
            f"/api/chat/sessions/{session_id}/artifacts",
            headers=self._super_headers(),
        )
        self.assertEqual(listing.status_code, 200)
        artifacts = listing.json()["artifacts"]
        self.assertEqual(len(artifacts), 2)
        self.assertEqual({a["name"] for a in artifacts}, {"solution"})
        self.assertNotEqual(artifacts[0]["artifact_uid"], artifacts[1]["artifact_uid"])

        # Independent histories: saving one never touches the other.
        save = self.client.post(
            f"/api/canvas/artifacts/{uid_a}/versions",
            json={"content": "a = 3\n", "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(save.status_code, 200)
        other = self.client.get(
            f"/api/canvas/artifacts/{uid_b}", headers=self._super_headers()
        )
        self.assertEqual(other.json()["current_version_no"], 1)

    def test_invalid_kind_rejected(self):
        session_id = self._make_session()
        response = self._create_artifact(session_id, kind="image")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "canvas_invalid_kind")


# ---------------------------------------------------------------------------
# Save / conflict / force (edge cases 2, 3)
# ---------------------------------------------------------------------------


class TestCanvasVersionConflicts(CanvasTestBase):
    def _make_artifact(self, content="v1\n"):
        session_id = self._make_session()
        response = self._create_artifact(session_id, content=content)
        self.assertEqual(response.status_code, 200)
        return response.json()["artifact_uid"]

    def test_save_conflict_409(self):
        uid = self._make_artifact()
        first = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "v2\n", "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["version_no"], 2)

        stale = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "v2-prime\n", "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"], "canvas_version_conflict")

        # No version lost: history is exactly [1, 2] with v2's content intact.
        versions = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        ).json()["versions"]
        self.assertEqual([v["version_no"] for v in versions], [1, 2])
        detail = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/2", headers=self._super_headers()
        )
        self.assertEqual(detail.json()["content"], "v2\n")

    def test_save_force_appends(self):
        uid = self._make_artifact()
        self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "v2\n", "base_version_no": 1},
            headers=self._super_headers(),
        )
        forced = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "v3-forced\n", "base_version_no": 1, "force": True},
            headers=self._super_headers(),
        )
        self.assertEqual(forced.status_code, 200, forced.text)
        # force appends at current+1 (2 -> 3), never overwriting history.
        self.assertEqual(forced.json()["version_no"], 3)
        detail = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        )
        self.assertEqual(detail.json()["current_version_no"], 3)
        versions = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        ).json()["versions"]
        self.assertEqual([v["version_no"] for v in versions], [1, 2, 3])
        self.assertEqual(
            self.client.get(
                f"/api/canvas/artifacts/{uid}/versions/3", headers=self._super_headers()
            ).json()["content"],
            "v3-forced\n",
        )

    def test_concurrent_saves_single_winner(self):
        """Two appends racing on the same base: exactly one 200, one 409."""
        uid = self._make_artifact()
        results = [
            self.client.post(
                f"/api/canvas/artifacts/{uid}/versions",
                json={"content": f"racer-{i}\n", "base_version_no": 1},
                headers=self._super_headers(),
            )
            for i in range(2)
        ]
        statuses = sorted(r.status_code for r in results)
        self.assertEqual(statuses, [200, 409])
        versions = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        ).json()["versions"]
        self.assertEqual(len(versions), 2)


# ---------------------------------------------------------------------------
# edit-range (edge cases 4, 10 partial, model errors, race)
# ---------------------------------------------------------------------------


class TestCanvasEditRange(CanvasTestBase):
    def _make_artifact(self, content):
        session_id = self._make_session()
        response = self._create_artifact(session_id, content=content)
        self.assertEqual(response.status_code, 200)
        return response.json()["artifact_uid"]

    def _edit(self, uid, start, end, instruction="rewrite", base=1, headers=None):
        return self.client.post(
            f"/api/canvas/artifacts/{uid}/edit-range",
            json={
                "start_line": start,
                "end_line": end,
                "instruction": instruction,
                "base_version_no": base,
            },
            headers=headers or self._super_headers(),
        )

    def test_edit_range_boundaries(self):
        content = "l1\nl2\nl3\nl4\nl5"
        cases = [
            ((1, 1), "X", "X\nl2\nl3\nl4\nl5", "first line"),
            ((5, 5), "X", "l1\nl2\nl3\nl4\nX", "last line"),
            ((3, 3), "X", "l1\nl2\nX\nl4\nl5", "single mid line"),
            ((2, 4), "A\nB", "l1\nA\nB\nl5", "multi-line replacement"),
        ]
        for (start, end), replacement, expected, label in cases:
            with self.subTest(case=label):
                self._install_llm(replacement)
                uid = self._make_artifact(content)
                response = self._edit(uid, start, end)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["origin"], "model_edit")
                detail = self.client.get(
                    f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
                )
                self.assertEqual(detail.json()["current_version"]["content"], expected)

    def test_edit_range_replacement_longer_than_range(self):
        """A legitimate edit may return more lines than the selected range."""
        self._install_llm("def a():\n    return 1")
        uid = self._make_artifact("x = 1\ny = 2")
        response = self._edit(uid, 1, 1)
        self.assertEqual(response.status_code, 200)
        content = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        ).json()["current_version"]["content"]
        self.assertEqual(content, "def a():\n    return 1\ny = 2")

    def test_edit_range_strips_markdown_fences(self):
        self._install_llm("```python\ndef fenced():\n    pass\n```")
        uid = self._make_artifact("x = 1\ny = 2")
        response = self._edit(uid, 1, 1)
        self.assertEqual(response.status_code, 200)
        content = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        ).json()["current_version"]["content"]
        self.assertEqual(content, "def fenced():\n    pass\ny = 2")

    def test_edit_range_invalid_range(self):
        uid = self._make_artifact("l1\nl2\nl3")
        for start, end in ((0, 1), (2, 1), (1, 4), (4, 5)):
            with self.subTest(start=start, end=end):
                response = self._edit(uid, start, end)
                self.assertEqual(response.status_code, 422)
                self.assertIn("canvas_invalid_range", response.json()["detail"])

    def test_edit_range_empty_instruction(self):
        uid = self._make_artifact("l1\nl2")
        response = self._edit(uid, 1, 1, instruction="   ")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "canvas_instruction_required")

    def test_edit_range_crlf_preserved(self):
        """Untouched lines keep their CR bytes; only the selected lines change."""
        self._install_llm("B")
        uid = self._make_artifact("a\r\nb\r\nc")
        response = self._edit(uid, 2, 2)
        self.assertEqual(response.status_code, 200, response.text)
        content = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        ).json()["current_version"]["content"]
        # Line 1 ("a\r") is untouched byte-for-byte; the replacement line has
        # no CR (replacement newlines come from the model, not the file).
        self.assertEqual(content, "a\r\nB\nc")

    def test_edit_range_trailing_newline_preserved(self):
        self._install_llm("A")
        uid = self._make_artifact("a\nb\n")  # trailing final newline
        response = self._edit(uid, 1, 1)
        self.assertEqual(response.status_code, 200)
        content = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        ).json()["current_version"]["content"]
        self.assertEqual(content, "A\nb\n")

    def test_edit_range_model_unavailable(self):
        uid = self._make_artifact("a\nb")
        mock = MagicMock()
        mock.chat_completion = AsyncMock(side_effect=LLMError("boom"))
        app.state.llm_client = mock
        response = self._edit(uid, 1, 1)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "canvas_model_unavailable")
        # No model_edit version was appended.
        versions = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        ).json()["versions"]
        self.assertEqual(len(versions), 1)

    def test_download_filename_degenerate_name_falls_back(self):
        """A degenerate artifact name (".") must not produce "..txt" — the
        filename falls back to the "canvas" stem (reviewer R-03)."""
        session_id = self._make_session()
        uid = self._create_artifact(
            session_id, name=".", headers=self._super_headers()
        ).json()["artifact_uid"]
        response = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/1/download",
            headers=self._super_headers(),
        )
        self.assertEqual(response.status_code, 200)
        disposition = response.headers["Content-Disposition"]
        self.assertIn('filename="canvas.txt"', disposition)

    def test_forked_session_starts_clean_original_untouched(self):
        """Pinned fork/reopen semantics (issue #509): a fork copies messages
        but NOT canvas artifacts — the fork lists none, the original keeps its
        history untouched, and a forked message can seed a NEW artifact in the
        forked session (turn lineage resolves there too)."""
        session_id = self._make_session()
        message_id = self._make_message(session_id, turn_id="t1")
        uid = self._create_artifact(
            session_id, message_id=message_id, headers=self._super_headers()
        ).json()["artifact_uid"]

        forked = self.client.post(
            f"/api/chat/sessions/{session_id}/fork",
            json={"message_index": 0},
            headers=self._super_headers(),
        )
        self.assertEqual(forked.status_code, 200)
        forked_body = forked.json()
        forked_id = forked_body["id"]

        forked_listing = self.client.get(
            f"/api/chat/sessions/{forked_id}/artifacts", headers=self._super_headers()
        )
        self.assertEqual(forked_listing.status_code, 200)
        self.assertEqual(forked_listing.json()["artifacts"], [])

        original = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        ).json()
        self.assertEqual(original["current_version_no"], 1)

        # Resolve the copied message id from the DB (the fork response shape
        # is chat's contract, not canvas's).
        conn = self._db()
        try:
            row = conn.execute(
                "SELECT id FROM chat_messages WHERE session_id = ? "
                "ORDER BY seq ASC, id ASC LIMIT 1",
                (forked_id,),
            ).fetchone()
        finally:
            self._release(conn)
        self.assertIsNotNone(row, "fork must copy the original session's messages")
        seeded = self._create_artifact(
            forked_id, message_id=row[0], headers=self._super_headers()
        )
        self.assertEqual(seeded.status_code, 200, seeded.text)
        self.assertIsNotNone(seeded.json()["turn_id"])

    def test_force_appends_unique_under_threads(self):
        """True multithreaded race (reviewer R-02): N threads force-append with
        the same stale base; every append must land on a UNIQUE version_no with
        no UNIQUE(artifact_id, version_no) violation and no lost version."""
        import threading

        session_id = self._make_session()
        uid = self._create_artifact(session_id, headers=self._super_headers()).json()[
            "artifact_uid"
        ]
        # The serialized artifact intentionally omits the internal rowid, so
        # resolve it through the store for the direct-append race below.
        artifact_id = CanvasStore().get_by_uid(uid)["id"]

        store = CanvasStore()
        results = []
        errors = []
        lock = threading.Barrier(5)

        def worker(i: int) -> None:
            try:
                lock.wait()
                version = store.append_version(
                    artifact_id,
                    content=f"thread {i}\n",
                    origin="user_edit",
                    created_by=1,
                    base_version_no=1,
                    force=True,
                )
                results.append(version)
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        version_nos = sorted(v["version_no"] for v in results)
        # Base was 1; the 5 forced appends occupy 2..6 with zero collisions.
        self.assertEqual(version_nos, [2, 3, 4, 5, 6])

    def test_edit_range_discarded_on_concurrent_save(self):
        """A save landing during model latency wins; the model output is
        discarded (409) rather than spliced on a stale base."""
        session_id = self._make_session()
        created = self._create_artifact(session_id, content="one\ntwo\n").json()
        uid = created["artifact_uid"]
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT id FROM canvas_artifacts WHERE artifact_uid = ?", (uid,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        artifact_id = row[0]
        store = CanvasStore()

        async def save_while_model_thinks(messages, **kwargs):
            # A concurrent user save lands while the model is "generating".
            store.append_version(
                artifact_id,
                content="one-and-a-half\ntwo\n",
                origin="user_edit",
                created_by=1,
                base_version_no=1,
            )
            return "MODEL OUTPUT"

        mock = MagicMock()
        mock.chat_completion = AsyncMock(side_effect=save_while_model_thinks)
        app.state.llm_client = mock

        response = self._edit(uid, 1, 1, base=1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "canvas_version_conflict")

        versions = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        ).json()["versions"]
        self.assertEqual(len(versions), 2)
        contents = [
            self.client.get(
                f"/api/canvas/artifacts/{uid}/versions/{v['version_no']}",
                headers=self._super_headers(),
            ).json()["content"]
            for v in versions
        ]
        # The model output must NOT appear anywhere in history.
        self.assertNotIn("MODEL OUTPUT", "".join(contents))
        self.assertIn("one-and-a-half", contents[1])

    def test_edit_range_records_model_edit_metadata(self):
        self._install_llm("Z")
        uid = self._make_artifact("p\nq")
        response = self._edit(uid, 1, 2, instruction="tidy up")
        self.assertEqual(response.status_code, 200)
        model_edit = response.json()["model_edit"]
        self.assertEqual(model_edit["start_line"], 1)
        self.assertEqual(model_edit["end_line"], 2)
        self.assertEqual(model_edit["instruction"], "tidy up")
        self.assertEqual(model_edit["base_version_no"], 1)


# ---------------------------------------------------------------------------
# Restore chain (edge case 5)
# ---------------------------------------------------------------------------


class TestCanvasRestore(CanvasTestBase):
    def test_restore_chain(self):
        session_id = self._make_session()
        uid = self._create_artifact(session_id, content="one\n").json()["artifact_uid"]
        for content in ("two\n", "three\n"):
            current = self.client.get(
                f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
            ).json()["current_version_no"]
            saved = self.client.post(
                f"/api/canvas/artifacts/{uid}/versions",
                json={"content": content, "base_version_no": current},
                headers=self._super_headers(),
            )
            self.assertEqual(saved.status_code, 200)

        # Restore the OLDEST version.
        first_restore = self.client.post(
            f"/api/canvas/artifacts/{uid}/restore",
            json={"version_no": 1, "base_version_no": 3},
            headers=self._super_headers(),
        )
        self.assertEqual(first_restore.status_code, 200, first_restore.text)
        self.assertEqual(first_restore.json()["origin"], "restore")
        self.assertEqual(first_restore.json()["version_no"], 4)
        self.assertEqual(first_restore.json()["content"], "one\n")
        # Restores carry no fabricated model provenance.
        self.assertIsNone(first_restore.json()["model_edit"])

        # Restore-of-restore: restoring the restored version appends again.
        second_restore = self.client.post(
            f"/api/canvas/artifacts/{uid}/restore",
            json={"version_no": 4, "base_version_no": 4},
            headers=self._super_headers(),
        )
        self.assertEqual(second_restore.status_code, 200)
        self.assertEqual(second_restore.json()["version_no"], 5)
        self.assertEqual(second_restore.json()["content"], "one\n")
        self.assertEqual(second_restore.json()["origin"], "restore")

        # History order preserved; original versions never destroyed.
        versions = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        ).json()["versions"]
        self.assertEqual([v["version_no"] for v in versions], [1, 2, 3, 4, 5])
        self.assertEqual(
            [v["origin"] for v in versions],
            ["created", "user_edit", "user_edit", "restore", "restore"],
        )
        oldest = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/1", headers=self._super_headers()
        )
        self.assertEqual(oldest.json()["content"], "one\n")

    def test_restore_unknown_version_404(self):
        session_id = self._make_session()
        uid = self._create_artifact(session_id).json()["artifact_uid"]
        response = self.client.post(
            f"/api/canvas/artifacts/{uid}/restore",
            json={"version_no": 99, "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "canvas_version_not_found")

    def test_restore_stale_base_409(self):
        session_id = self._make_session()
        uid = self._create_artifact(session_id).json()["artifact_uid"]
        saved = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "v2\n", "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(saved.status_code, 200)
        response = self.client.post(
            f"/api/canvas/artifacts/{uid}/restore",
            json={"version_no": 1, "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(response.status_code, 409)


# ---------------------------------------------------------------------------
# Download + export (edge cases 6, 11, 14)
# ---------------------------------------------------------------------------


class TestCanvasDownloadExport(CanvasTestBase):
    def test_download_bytes_exact(self):
        content = "def f():\r\n    return 'x'\r\n"  # CRLF + trailing newline
        session_id = self._make_session()
        message_id = self._make_message(session_id, turn_id="turn-abc")
        created = self._create_artifact(
            session_id,
            content=content,
            name="My Module",
            language="py",
            message_id=message_id,
            source_refs=[{"label": "[S1]", "title": "Source One"}],
        ).json()
        uid = created["artifact_uid"]

        # Save a second version to prove the download pins the REQUESTED
        # version's bytes, not the current one.
        self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "changed\n", "base_version_no": 1},
            headers=self._super_headers(),
        )

        response = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/1/download",
            headers=self._super_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content.encode("utf-8"))
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/x-python")
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="My_Module.py"',
        )
        self.assertEqual(response.headers["x-canvas-session-id"], str(session_id))
        self.assertEqual(response.headers["x-canvas-turn-id"], "turn-abc")
        self.assertEqual(response.headers["x-canvas-origin-message-id"], str(message_id))
        refs = json.loads(
            unquote(response.headers["x-canvas-source-refs"])
        )
        self.assertEqual(refs, [{"label": "[S1]", "title": "Source One"}])

    def test_download_omits_null_lineage_headers(self):
        session_id = self._make_session()
        uid = self._create_artifact(session_id, content="plain text\n").json()["artifact_uid"]
        response = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/1/download",
            headers=self._super_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/plain")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertTrue(
            response.headers["content-disposition"].endswith('.txt"')
        )
        self.assertNotIn("x-canvas-turn-id", response.headers)
        self.assertNotIn("x-canvas-origin-message-id", response.headers)
        self.assertNotIn("x-canvas-source-refs", response.headers)

    def test_content_byte_exactness(self):
        """LF content without a trailing newline survives create/save/download
        byte-for-byte (no normalization pass anywhere)."""
        session_id = self._make_session()
        content = "no trailing newline"
        uid = self._create_artifact(session_id, content=content).json()["artifact_uid"]
        edited = "still no trailing newline"
        saved = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": edited, "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(saved.status_code, 200)
        download = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/2/download",
            headers=self._super_headers(),
        )
        self.assertEqual(download.content, edited.encode("utf-8"))

    def test_export_manifest_fields(self):
        session_id = self._make_session()
        message_id = self._make_message(session_id, turn_id="turn-xyz")
        refs = [{"label": "[S1]", "title": "Source One"}]
        created = self._create_artifact(
            session_id,
            content="manifest body\n",
            name="doc",
            kind="document",
            language="md",
            message_id=message_id,
            source_refs=refs,
        ).json()
        uid = created["artifact_uid"]

        manifest_response = self.client.get(
            f"/api/canvas/artifacts/{uid}/export", headers=self._super_headers()
        )
        self.assertEqual(manifest_response.status_code, 200)
        manifest = manifest_response.json()
        self.assertEqual(
            set(manifest.keys()),
            {
                "artifact_uid",
                "kind",
                "name",
                "language",
                "version_no",
                "version_name",
                "content",
                "content_sha256",
                "source_refs",
                "session_id",
                "turn_id",
                "message_id",
                "exported_at",
            },
        )
        self.assertEqual(manifest["artifact_uid"], uid)
        self.assertEqual(manifest["kind"], "document")
        self.assertEqual(manifest["version_no"], 1)
        self.assertEqual(manifest["content"], "manifest body\n")
        self.assertEqual(manifest["source_refs"], refs)
        self.assertEqual(manifest["session_id"], session_id)
        self.assertEqual(manifest["turn_id"], "turn-xyz")
        self.assertEqual(manifest["message_id"], message_id)
        self.assertTrue(manifest["exported_at"].endswith("Z"))
        self.assertEqual(
            manifest["content_sha256"],
            hashlib.sha256("manifest body\n".encode()).hexdigest(),
        )

        # Explicit version_no selects a historical version.
        self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "v2 body\n", "base_version_no": 1},
            headers=self._super_headers(),
        )
        historical = self.client.get(
            f"/api/canvas/artifacts/{uid}/export?version_no=1",
            headers=self._super_headers(),
        ).json()
        self.assertEqual(historical["content"], "manifest body\n")
        self.assertEqual(historical["version_no"], 1)
        missing = self.client.get(
            f"/api/canvas/artifacts/{uid}/export?version_no=9",
            headers=self._super_headers(),
        )
        self.assertEqual(missing.status_code, 404)

    def test_citation_markers_preserved(self):
        """Literal [S1] markers survive create + download verbatim."""
        content = "Answer with citation [S1] and [S2] markers\nsecond [S1]\n"
        session_id = self._make_session()
        uid = self._create_artifact(session_id, content=content).json()["artifact_uid"]
        detail = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        ).json()
        self.assertEqual(detail["current_version"]["content"], content)
        download = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/1/download",
            headers=self._super_headers(),
        )
        self.assertEqual(download.content, content.encode("utf-8"))

    def test_utf8_content_roundtrip(self):
        content = "def 数学():\n    return '🎉 café'\n"
        session_id = self._make_session()
        uid = self._create_artifact(session_id, content=content).json()["artifact_uid"]
        self._install_llm("    return '🎊'")
        edited = self.client.post(
            f"/api/canvas/artifacts/{uid}/edit-range",
            json={
                "start_line": 2,
                "end_line": 2,
                "instruction": "swap the emoji",
                "base_version_no": 1,
            },
            headers=self._super_headers(),
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        download = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/2/download",
            headers=self._super_headers(),
        )
        self.assertEqual(
            download.content, "def 数学():\n    return '🎊'\n".encode("utf-8")
        )


# ---------------------------------------------------------------------------
# Create validation (edge cases 12, 13, 15, 16)
# ---------------------------------------------------------------------------


class TestCanvasCreateValidation(CanvasTestBase):
    def test_create_message_id_validation(self):
        session_id = self._make_session()
        other_session = self._make_session(title="Other")
        wrong_session_message = self._make_message(other_session)

        missing = self._create_artifact(session_id, message_id=99999)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "canvas_message_not_found")

        cross = self._create_artifact(session_id, message_id=wrong_session_message)
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(cross.json()["detail"], "canvas_message_not_found")

        valid = self._create_artifact(
            session_id, message_id=self._make_message(session_id)
        )
        self.assertEqual(valid.status_code, 200)

        missing_session = self._create_artifact(99999)
        self.assertEqual(missing_session.status_code, 404)
        self.assertEqual(missing_session.json()["detail"], "canvas_session_not_found")

    def test_empty_content_rejected(self):
        session_id = self._make_session()
        for content in ("", "   \n\t  "):
            with self.subTest(repr(content)):
                response = self._create_artifact(session_id, content=content)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "canvas_content_required")

        # Save path enforces the same guard.
        uid = self._create_artifact(session_id).json()["artifact_uid"]
        save = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "  \n ", "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(save.status_code, 422)
        self.assertEqual(save.json()["detail"], "canvas_content_required")

    def test_oversize_content_rejected(self):
        settings.canvas_max_artifact_kb = 1
        session_id = self._make_session()
        big = "x" * (1 * 1024 + 1)
        response = self._create_artifact(session_id, content=big)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "canvas_artifact_too_large")

        settings.canvas_max_artifact_kb = 512
        uid = self._create_artifact(session_id).json()["artifact_uid"]
        settings.canvas_max_artifact_kb = 1
        save = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": big, "base_version_no": 1},
            headers=self._super_headers(),
        )
        self.assertEqual(save.status_code, 413)

    def test_source_refs_snapshot(self):
        """source_refs are a create-time snapshot; later message changes (or
        deletion) never rewrite the stored refs."""
        session_id = self._make_session()
        message_id = self._make_message(session_id)
        refs = [{"label": "[S1]", "title": "Original source"}]
        uid = self._create_artifact(
            session_id, message_id=message_id, source_refs=refs
        ).json()["artifact_uid"]

        conn = self._db()
        try:
            conn.execute("DELETE FROM chat_messages WHERE id = ?", (message_id,))
            conn.commit()
        finally:
            self._release(conn)

        detail = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._super_headers()
        ).json()
        self.assertEqual(detail["source_refs"], refs)
        manifest = self.client.get(
            f"/api/canvas/artifacts/{uid}/export", headers=self._super_headers()
        ).json()
        self.assertEqual(manifest["source_refs"], refs)

    def test_empty_source_refs_defaults_to_empty_list(self):
        session_id = self._make_session()
        created = self._create_artifact(session_id).json()
        self.assertEqual(created["source_refs"], [])

    def test_turn_id_resolved_from_message(self):
        session_id = self._make_session()
        message_id = self._make_message(session_id, turn_id="server-turn")

        # Server value wins even when the client also sends a turn_id.
        created = self._create_artifact(
            session_id, message_id=message_id, turn_id="client-turn"
        ).json()
        self.assertEqual(created["turn_id"], "server-turn")

        # Client value honored only when message_id is absent.
        client_only = self._create_artifact(session_id, turn_id="client-turn").json()
        self.assertEqual(client_only["turn_id"], "client-turn")

        neither = self._create_artifact(session_id).json()
        self.assertIsNone(neither["turn_id"])


# ---------------------------------------------------------------------------
# Authorization matrix (edge case 8) + disabled flag (edge case 9)
# ---------------------------------------------------------------------------


class TestCanvasAuthorization(CanvasTestBase):
    def test_canvas_authz_matrix(self):
        session_id = self._make_session()
        uid = self._create_artifact(session_id, headers=self._super_headers()).json()[
            "artifact_uid"
        ]

        # Unauthenticated -> 401.
        unauth = self.client.get(f"/api/canvas/artifacts/{uid}")
        self.assertEqual(unauth.status_code, 401)

        # Unknown uid -> 404 BEFORE policy (no vault existence oracle).
        unknown = self.client.get(
            "/api/canvas/artifacts/cav_doesnotexist", headers=self._super_headers()
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["detail"], "canvas_artifact_not_found")

        # User without vault access -> 403 on read.
        denied_read = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._no_access_headers()
        )
        self.assertEqual(denied_read.status_code, 403)
        self.assertIn("No read access", denied_read.json()["detail"])

        # Read-only member -> 200 read, 403 write.
        read_ok = self.client.get(
            f"/api/canvas/artifacts/{uid}", headers=self._member_headers()
        )
        self.assertEqual(read_ok.status_code, 200)
        write_denied = self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "nope\n", "base_version_no": 1},
            headers=self._member_headers(),
        )
        self.assertEqual(write_denied.status_code, 403)
        self.assertIn("No write access", write_denied.json()["detail"])

        # Creating an artifact is a session-scoped mutation: it requires vault
        # WRITE (mirrors chat add_message/batch/truncate/feedback), so a
        # read-only member is denied even though listing is allowed.
        create_denied = self.client.post(
            f"/api/chat/sessions/{session_id}/artifacts",
            json={"kind": "code", "name": "n", "content": "x = 1\n"},
            headers=self._member_headers(),
        )
        self.assertEqual(create_denied.status_code, 403)
        self.assertIn("No write access", create_denied.json()["detail"])

        # Listing another user's session still allowed with vault read
        # (vault policy scopes access, mirroring chat get_session).
        listing = self.client.get(
            f"/api/chat/sessions/{session_id}/artifacts",
            headers=self._member_headers(),
        )
        self.assertEqual(listing.status_code, 200)

        no_access_listing = self.client.get(
            f"/api/chat/sessions/{session_id}/artifacts",
            headers=self._no_access_headers(),
        )
        self.assertEqual(no_access_listing.status_code, 403)

    def test_unknown_session_404_on_create(self):
        response = self._create_artifact(424242)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "canvas_session_not_found")


class TestCanvasDisabled(CanvasTestBase):
    def test_canvas_disabled_503(self):
        settings.canvas_enabled = False
        session_id = self._make_session()
        headers = self._super_headers()

        checks = [
            self.client.get("/api/canvas/capabilities", headers=headers),
            self.client.post(
                f"/api/chat/sessions/{session_id}/artifacts",
                json={"kind": "code", "name": "n", "content": "x\n"},
                headers=headers,
            ),
            self.client.get(
                f"/api/chat/sessions/{session_id}/artifacts", headers=headers
            ),
            self.client.get("/api/canvas/artifacts/cav_x", headers=headers),
            self.client.get("/api/canvas/artifacts/cav_x/versions", headers=headers),
            self.client.get("/api/canvas/artifacts/cav_x/versions/1", headers=headers),
            self.client.post(
                "/api/canvas/artifacts/cav_x/versions",
                json={"content": "y\n", "base_version_no": 1},
                headers=headers,
            ),
            self.client.post(
                "/api/canvas/artifacts/cav_x/restore",
                json={"version_no": 1, "base_version_no": 1},
                headers=headers,
            ),
            self.client.post(
                "/api/canvas/artifacts/cav_x/edit-range",
                json={
                    "start_line": 1,
                    "end_line": 1,
                    "instruction": "i",
                    "base_version_no": 1,
                },
                headers=headers,
            ),
            self.client.get(
                "/api/canvas/artifacts/cav_x/versions/1/download", headers=headers
            ),
            self.client.get("/api/canvas/artifacts/cav_x/export", headers=headers),
        ]
        for response in checks:
            self.assertEqual(response.status_code, 503, response.url)
            self.assertEqual(response.json()["detail"], "canvas_disabled")


# ---------------------------------------------------------------------------
# Version list shape (edge case 18)
# ---------------------------------------------------------------------------


class TestCanvasVersionListShape(CanvasTestBase):
    def test_version_list_shape(self):
        session_id = self._make_session()
        uid = self._create_artifact(session_id, content="a\nb\nc\n").json()["artifact_uid"]
        self.client.post(
            f"/api/canvas/artifacts/{uid}/versions",
            json={"content": "a2\nb\nc\n", "base_version_no": 1, "name": "named save"},
            headers=self._super_headers(),
        )
        self._install_llm("B2")
        self.client.post(
            f"/api/canvas/artifacts/{uid}/edit-range",
            json={
                "start_line": 2,
                "end_line": 2,
                "instruction": "fix b",
                "base_version_no": 2,
            },
            headers=self._super_headers(),
        )

        versions = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        ).json()["versions"]
        self.assertEqual(len(versions), 3)
        for version in versions:
            # Content bodies must NOT ship in the list shape.
            self.assertNotIn("content", version)
        self.assertEqual([v["version_no"] for v in versions], [1, 2, 3])
        self.assertEqual([v["origin"] for v in versions], ["created", "user_edit", "model_edit"])
        self.assertEqual(versions[1]["name"], "named save")
        self.assertIsNone(versions[0]["model_edit"])
        self.assertIsNone(versions[1]["model_edit"])
        self.assertEqual(versions[2]["model_edit"]["start_line"], 2)
        self.assertEqual(versions[2]["model_edit"]["end_line"], 2)
        self.assertEqual(versions[2]["model_edit"]["instruction"], "fix b")
        self.assertEqual(versions[2]["model_edit"]["base_version_no"], 2)
        self.assertTrue(all(v["created_at"] for v in versions))


class TestCanvasRouterRegistration(CanvasTestBase):
    def test_canvas_routes_registered(self):
        """Router smoke: every canvas route is registered and visible in the
        OpenAPI schema (asserted black-box through /openapi.json rather than
        app.routes internals, which vary by FastAPI version)."""
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema_paths = set(response.json()["paths"].keys())
        expected = {
            "/api/chat/sessions/{session_id}/artifacts",
            "/api/canvas/capabilities",
            "/api/canvas/artifacts/{artifact_uid}",
            "/api/canvas/artifacts/{artifact_uid}/versions",
            "/api/canvas/artifacts/{artifact_uid}/versions/{version_no}",
            "/api/canvas/artifacts/{artifact_uid}/restore",
            "/api/canvas/artifacts/{artifact_uid}/edit-range",
            "/api/canvas/artifacts/{artifact_uid}/versions/{version_no}/download",
            "/api/canvas/artifacts/{artifact_uid}/export",
        }
        missing = expected - schema_paths
        self.assertFalse(missing, f"canvas routes not registered: {missing}")


if __name__ == "__main__":
    unittest.main()
