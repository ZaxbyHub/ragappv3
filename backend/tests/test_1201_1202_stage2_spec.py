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
from app.api.routes import quality_reports as qr  # noqa: E402
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

    def test_submitter_unique_race_loser_note_is_folded(self):
        # Two submissions can both miss the pre-SELECT; the loser of the
        # unique-index race must still fold its note and submitter into the
        # canonical winner, not silently disappear.
        class _NoRows:
            def fetchone(self):
                return None

        class _UniqueRaceProxy:
            def __init__(self, conn):
                self.conn = conn
                self.fired = False
                self.selects = 0

            def execute(self, sql, params=()):
                if "SELECT * FROM quality_reports" in sql and "message_id = ?" in sql:
                    self.selects += 1
                    if self.selects == 1:  # this racer misses the pre-SELECT
                        return _NoRows()
                if "INSERT INTO quality_reports" in sql and not self.fired:
                    self.fired = True
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: quality_reports.message_id, quality_reports.category"
                    )
                return self.conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.conn, name)

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
        proxy = _UniqueRaceProxy(self.conn)
        app.dependency_overrides[get_db] = lambda: proxy
        try:
            raced = self.client.post(
                "/api/quality/reports", json={**body, "note": "racing second note"}
            )
        finally:
            app.dependency_overrides[get_db] = lambda: None
        self.assertTrue(proxy.fired, "the racing INSERT never ran")
        self.assertEqual(raced.status_code, 200, raced.text)
        note = raced.json()["note"]
        self.assertIn("first note", note)
        self.assertIn("racing second note", note)
        self.assertIn(str(SECOND_ADMIN["id"]), note)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM quality_reports").fetchone()[0], 1
        )


class NoteMergeCasTest(unittest.TestCase):
    def test_submitter_note_merge_is_compare_and_swap(self):
        merge = getattr(qr, "_merge_report_note_cas", None)
        assert callable(merge), "note merge has no compare-and-swap helper"

        class _Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class _Update:
            def __init__(self, rowcount):
                self.rowcount = rowcount

        class _FakeConn:
            def __init__(self):
                self.note = "first note"
                self.lost_once = False

            def execute(self, sql, params=()):
                if sql.startswith("SELECT note"):
                    return _Cursor((self.note,))
                if sql.startswith("UPDATE quality_reports"):
                    if (
                        not self.lost_once
                    ):  # a competing writer lands between read and CAS
                        self.lost_once = True
                        self.note = "competing note"
                        return _Update(0)
                    self.note = params[0]
                    return _Update(1)
                raise AssertionError(sql)

            def commit(self):
                return None

        conn = _FakeConn()
        merge(conn, 1, "racing second note", SECOND_ADMIN["id"])
        self.assertIn("competing note", conn.note)
        self.assertIn("racing second note", conn.note)
        self.assertIn(str(SECOND_ADMIN["id"]), conn.note)

    def test_submitter_note_merge_handles_stored_null(self):
        # A first submission may legitimately omit the note (SQL NULL); a
        # later differing note must still merge instead of hitting the CAS
        # retry limit because NULL != ''.
        merge = getattr(qr, "_merge_report_note_cas", None)
        assert callable(merge), "note merge has no compare-and-swap helper"
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "r.db")
            init_db(path)
            run_migrations(path)
            conn = sqlite3.connect(path)
            try:
                self._seed_null_note(conn)
                merge(conn, 1, "later note", SECOND_ADMIN["id"])
                note = conn.execute(
                    "SELECT note FROM quality_reports WHERE id = 1"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertIn("later note", note)
            self.assertIn(str(SECOND_ADMIN["id"]), note)

    def _seed_null_note(self, conn):
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (?, 'a', 'x', 'admin', 1)",
            (ADMIN["id"],),
        )
        conn.execute(
            "INSERT INTO vaults (id, name, description) VALUES (?, 'v', 'v')", (VAULT,)
        )
        sid = conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, 'S')",
            (VAULT, ADMIN["id"]),
        ).lastrowid
        mid = conn.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (?, 'user', 'q')",
            (sid,),
        ).lastrowid
        conn.execute(
            "INSERT INTO quality_reports (id, session_id, message_id, category, note,"
            " config_ref, source_file_hashes) VALUES (1, ?, ?, 'incorrect_answer', NULL, 'x', '[]')",
            (sid, mid),
        )
        conn.commit()
        return sid, mid


class MergeVanishedReportTest(_Base):
    def test_submitter_merge_vanished_report_returns_404(self):
        class _VanishingProxy:
            def __init__(self, conn):
                self.conn = conn
                self.fired = False

            def execute(self, sql, params=()):
                if (
                    sql.startswith("SELECT note FROM quality_reports")
                    and not self.fired
                ):
                    self.fired = True
                    self.conn.execute("DELETE FROM quality_reports")
                    self.conn.commit()
                return self.conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        sid, mid = self._session()
        body = {
            "session_id": sid,
            "message_id": mid,
            "category": "incorrect_answer",
            "note": "first note",
        }
        self.assertEqual(
            self.client.post("/api/quality/reports", json=body).status_code, 200
        )
        self.current = SECOND_ADMIN
        proxy = _VanishingProxy(self.conn)
        app.dependency_overrides[get_db] = lambda: proxy
        try:
            raced = self.client.post(
                "/api/quality/reports", json={**body, "note": "second note"}
            )
        finally:
            app.dependency_overrides[get_db] = lambda: None
        self.assertTrue(proxy.fired)
        self.assertEqual(raced.status_code, 404, raced.text)
        self.assertIn("Quality report not found", raced.text)


class ReportNoteBoundsTest(_Base):
    def test_reserved_separator_and_cap_are_enforced(self):
        sid, mid = self._session()
        reserved = self.client.post(
            "/api/quality/reports",
            json={
                "session_id": sid,
                "message_id": mid,
                "category": "incorrect_answer",
                "note": "first || second",
            },
        )
        self.assertEqual(reserved.status_code, 422, reserved.text)
        self.assertIn("reserved separator", reserved.text)

        cap = settings.chat_message_max_chars
        max_note = qr._note_body_limit()
        self.current = ADMIN
        bounded_first = self.client.post(
            "/api/quality/reports",
            json={
                "session_id": sid,
                "message_id": mid,
                "category": "stale_source",
                "note": "a" * max_note,
            },
        )
        self.assertEqual(bounded_first.status_code, 200, bounded_first.text)
        self.current = SECOND_ADMIN
        bounded_second = self.client.post(
            "/api/quality/reports",
            json={
                "session_id": sid,
                "message_id": mid,
                "category": "stale_source",
                "note": "b" * max_note,
            },
        )
        self.assertEqual(bounded_second.status_code, 200, bounded_second.text)
        bounded_note = self.conn.execute(
            "SELECT note FROM quality_reports WHERE message_id = ? "
            "AND category = 'stale_source'",
            (mid,),
        ).fetchone()[0]
        self.assertLessEqual(len(bounded_note), qr.QUALITY_REPORT_NOTE_MAX_CHARS)
        self.assertIn("(submitted by user 7)", bounded_note)


        overlong = self.client.post(
            "/api/quality/reports",
            json={
                "session_id": sid,
                "message_id": mid,
                "category": "missing_source",
                "note": "x" * (max_note + 1),
            },
        )
        self.assertEqual(overlong.status_code, 422, overlong.text)

    def test_full_canonical_note_overflow_is_rejected_through_route(self):
        sid, mid = self._session()
        cap = qr.QUALITY_REPORT_NOTE_MAX_CHARS
        full_note = "x" * cap
        self.conn.execute(
            "INSERT INTO quality_reports (session_id, message_id, category, note, "
            "turn_id, seq, config_ref, source_file_hashes) VALUES (?, ?, ?, ?, 't', 1, 'x', '[]')",
            (sid, mid, "bad_extraction", full_note),
        )
        self.conn.commit()
        self.current = SECOND_ADMIN
        response = self.client.post(
            "/api/quality/reports",
            json={
                "session_id": sid,
                "message_id": mid,
                "category": "bad_extraction",
                "note": "another note",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("report-note cap", response.text)
        stored = self.conn.execute(
            "SELECT note FROM quality_reports WHERE message_id = ? "
            "AND category = 'bad_extraction'",
            (mid,),
        ).fetchone()[0]
        self.assertEqual(stored, full_note)

    def test_existing_note_folds_and_short_retry_is_idempotent(self):
        sid, mid = self._session()
        cap = settings.chat_message_max_chars
        self.conn.execute(
            "INSERT INTO quality_reports (session_id, message_id, category, note, "
            "turn_id, seq, config_ref, source_file_hashes) VALUES (?, ?, ?, ?, 't', 1, 'x', '[]')",
            (sid, mid, "missing_source", "x" * cap),
        )
        self.conn.commit()
        self.current = SECOND_ADMIN
        overflow = self.client.post(
            "/api/quality/reports",
            json={
                "session_id": sid,
                "message_id": mid,
                "category": "missing_source",
                "note": "another note",
            },
        )
        self.assertEqual(overflow.status_code, 200, overflow.text)
        folded_overflow = self.conn.execute(
            "SELECT note FROM quality_reports WHERE message_id = ? "
            "AND category = 'missing_source'",
            (mid,),
        ).fetchone()[0]
        self.assertLessEqual(len(folded_overflow), qr.QUALITY_REPORT_NOTE_MAX_CHARS)
        self.assertIn("another note (submitted by user 7)", folded_overflow)

        self.current = ADMIN
        first_body = {
            "session_id": sid,
            "message_id": mid,
            "category": "bad_extraction",
            "note": "short note",
        }
        first = self.client.post("/api/quality/reports", json=first_body)
        self.assertEqual(first.status_code, 200, first.text)
        self.current = SECOND_ADMIN
        body = {**first_body, "note": "folded note"}
        folded = self.client.post("/api/quality/reports", json=body)
        self.assertEqual(folded.status_code, 200, folded.text)
        stored = self.conn.execute(
            "SELECT note FROM quality_reports WHERE message_id = ? "
            "AND category = 'bad_extraction'",
            (mid,),
        ).fetchone()[0]
        self.assertEqual(stored, "folded note (submitted by user 7) || short note")
        retry = self.client.post("/api/quality/reports", json=body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(
            self.conn.execute(
                "SELECT note FROM quality_reports WHERE message_id = ? "
                "AND category = 'bad_extraction'",
                (mid,),
            ).fetchone()[0],
            stored,
        )


class SettingsWireTest(_Base):
    def test_assistant_cap_update_and_read_round_trip(self):
        original = settings.assistant_message_max_chars
        try:
            updated = self.client.put(
                "/api/settings",
                json={"assistant_message_max_chars": 99999},
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(
                updated.json()["assistant_message_max_chars"], 99999
            )
            read_back = self.client.get("/api/settings")
            self.assertEqual(read_back.status_code, 200, read_back.text)
            self.assertEqual(
                read_back.json()["assistant_message_max_chars"], 99999
            )
        finally:
            restored = self.client.put(
                "/api/settings",
                json={"assistant_message_max_chars": original},
            )
            self.assertEqual(restored.status_code, 200, restored.text)


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
        ceiling = max(
            settings.assistant_message_max_chars,
            settings.thinking_max_tokens * 8,
        )
        self.assertGreater(ceiling, settings.chat_message_max_chars)
        sid, _mid = self._session()
        # A legitimate thinking-mode answer can be many characters per token.
        legitimate = "a" * 40000
        long_answer = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={
                "messages": [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": legitimate},
                ]
            },
        )
        self.assertEqual(long_answer.status_code, 200, long_answer.text)
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

    def test_assistant_cap_tracks_live_thinking_budget_and_reconcile_is_exempt(self):
        """The backstop follows a raised thinking budget at check time, and a
        #553 reconcile of a row the server already persisted is never rejected
        by the backstop after the budget is lowered again.
        """
        original = settings.thinking_max_tokens
        self.addCleanup(setattr, settings, "thinking_max_tokens", original)
        settings.thinking_max_tokens = 65536
        sid, _mid = self._session()
        long_answer = "a" * (settings.assistant_message_max_chars + 1)
        turn_id = "round-trip-live-budget"
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": long_answer,
                    "turn_id": turn_id,
                }
            ]
        }
        first = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch", json=payload
        )
        self.assertEqual(first.status_code, 200, first.text)

        # A fresh row of the same size would now exceed the floor backstop.
        settings.thinking_max_tokens = 1
        fresh = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={"messages": [{"role": "assistant", "content": long_answer}]},
        )
        self.assertEqual(fresh.status_code, 422, fresh.text)

        # The durable row reconciles in place when the re-post does not grow
        # past the length the server already persisted, even under the lower
        # budget. A client-bootstrapped row cannot grow into an uncapped body.
        retry = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch", json=payload
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        rows = self.conn.execute(
            "SELECT id, content FROM chat_messages "
            "WHERE session_id = ? AND turn_id = ? AND role = 'assistant'",
            (sid, turn_id),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0][1]), len(long_answer))

        bootstrap_turn = "client-bootstrapped-small-row"
        bootstrap = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={
                "messages": [
                    {
                        "role": "assistant",
                        "content": "seed",
                        "turn_id": bootstrap_turn,
                    }
                ]
            },
        )
        self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
        growth = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={
                "messages": [
                    {
                        "role": "assistant",
                        "content": long_answer,
                        "turn_id": bootstrap_turn,
                    }
                ]
            },
        )
        self.assertEqual(growth.status_code, 422, growth.text)
        stored = self.conn.execute(
            "SELECT content FROM chat_messages "
            "WHERE session_id = ? AND turn_id = ? AND role = 'assistant'",
            (sid, bootstrap_turn),
        ).fetchone()[0]
        self.assertEqual(stored, "seed")
