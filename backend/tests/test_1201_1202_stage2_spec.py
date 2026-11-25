"""Stage 2 frozen specification tests for the maintainer-resolved decisions.

Selectors:
- `submitter` — AC3 fold-merge submission attribution (RED at base).
- `share_link_pin` — #573 cross-user GET stays allowed (GREEN at base).
- `assistant_cap` — generous assistant-role ceiling on the write routes (RED at base).
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
)
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402
from app.security import csrf_protect  # noqa: E402

ADMIN = {"id": 1, "username": "admin1", "role": "admin", "is_active": True}
SECOND_ADMIN = {"id": 7, "username": "admin2", "role": "admin", "is_active": True}
MEMBER = {"id": 3, "username": "member1", "role": "member", "is_active": True}
COWORKER = {"id": 4, "username": "member2", "role": "member", "is_active": True}
VAULT = 301


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stage2-spec-")
        self.db = str(Path(self.tmp) / "app.db")
        init_db(self.db)
        run_migrations(self.db)
        self.pool = SimpleConnectionPool(self.db)
        self.conn = sqlite3.connect(self.db, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        for user in (ADMIN, SECOND_ADMIN, MEMBER, COWORKER):
            self.conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, role, is_active)"
                " VALUES (?, ?, 'x', ?, 1)",
                (user["id"], user["username"], user["role"]),
            )
        self.conn.execute(
            "INSERT INTO vaults (id, name, description) VALUES (?, 'v', 'v')", (VAULT,)
        )
        self.conn.executemany(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, 'write')",
            [
                (VAULT, ADMIN["id"]),
                (VAULT, SECOND_ADMIN["id"]),
                (VAULT, MEMBER["id"]),
                (VAULT, COWORKER["id"]),
            ],
        )
        self.conn.commit()
        self.current = ADMIN

        async def evaluate(
            user, resource_type=None, resource_id=None, action=None, *a, **k
        ):
            row = self.conn.execute(
                "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = ?",
                (resource_id, user["id"]),
            ).fetchone()
            order = {"read": 0, "write": 1, "admin": 2}
            return row is not None and order.get(row[0], -1) >= order.get(action, 99)

        def override_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_active_user] = lambda: self.current
        app.dependency_overrides[get_evaluate_policy] = lambda: evaluate
        app.dependency_overrides[csrf_protect] = lambda: "t"
        app.dependency_overrides[get_rag_engine] = lambda: MagicMock(llm_client=None)
        self.addCleanup(self._cleanup)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _cleanup(self):
        for dep in (
            get_db,
            get_current_active_user,
            get_evaluate_policy,
            csrf_protect,
            get_rag_engine,
        ):
            app.dependency_overrides.pop(dep, None)
        self.conn.close()
        self.pool.close_all()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self, owner_id=ADMIN["id"]):
        sid = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at)"
            " VALUES (?, ?, 'S', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (VAULT, owner_id),
        ).lastrowid
        mid = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, seq, created_at)"
            " VALUES (?, 'user', 'q', 1, CURRENT_TIMESTAMP)",
            (sid,),
        ).lastrowid
        self.conn.commit()
        return sid, mid


class SubmitterAttributionTest(_Base):
    def test_submitter_differing_note_is_fold_merged_with_attribution(self):
        """A second authorized user's differing note on an identical
        `(message_id, category)` identity is folded into the canonical note
        with the existing ` || ` convention and the second submitter's id.
        One row remains, and a retry is idempotent.
        """
        sid, mid = self._session()
        body = {
            "session_id": sid,
            "message_id": mid,
            "category": "incorrect_answer",
            "note": "first note",
        }
        first = self.client.post("/api/quality/reports", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.current = SECOND_ADMIN
        second = self.client.post(
            "/api/quality/reports", json={**body, "note": "second note"}
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["id"], first.json()["id"])
        note = second.json()["note"]
        self.assertIn("first note", note)
        self.assertIn("second note", note)
        self.assertIn(" || ", note)
        self.assertIn(str(SECOND_ADMIN["id"]), note)
        rows = self.conn.execute("SELECT COUNT(*) FROM quality_reports").fetchone()[0]
        self.assertEqual(rows, 1)
        retry = self.client.post(
            "/api/quality/reports", json={**body, "note": "second note"}
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["note"], note)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM quality_reports").fetchone()[0], 1
        )


class ShareLinkPinTest(_Base):
    def test_share_link_pin_cross_user_get_stays_allowed(self):
        """#573's share link stays functional: a member with vault read can
        open another member's session by id after the owner rule is added.
        """
        sid, _mid = self._session(owner_id=ADMIN["id"])
        self.current = COWORKER
        resp = self.client.get(f"/api/chat/sessions/{sid}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["id"], sid)


class AssistantCapTest(_Base):
    def test_assistant_cap_enforced_on_both_write_routes(self):
        """A generous ceiling derived from thinking_max_tokens bounds
        assistant-role items on both write routes; exact-cap content passes
        and the user cap is unchanged.
        """
        ceiling = getattr(
            settings, "assistant_message_max_chars", settings.thinking_max_tokens
        )
        self.assertGreater(ceiling, settings.chat_message_max_chars)
        sid, _mid = self._session()
        exact = "a" * ceiling
        r = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "assistant", "content": exact},
        )
        self.assertEqual(r.status_code, 200, r.text)
        over = "a" * (ceiling + 1)
        single = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "assistant", "content": over},
        )
        self.assertEqual(single.status_code, 422, single.text)
        self.assertIn("assistant_message_max_chars", single.text)
        batch = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={"messages": [{"role": "assistant", "content": over}]},
        )
        self.assertEqual(batch.status_code, 422, batch.text)
        user_item = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={
                "role": "user",
                "content": "q" * (settings.chat_message_max_chars + 1),
            },
        )
        self.assertEqual(user_item.status_code, 422, user_item.text)
