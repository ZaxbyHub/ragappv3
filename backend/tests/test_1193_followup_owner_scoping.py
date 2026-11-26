"""Frozen specification tests for the PR #1193 follow-up (#1201 side).

Owner scoping of the mutating chat-session surfaces (AC1), the legacy
NULL-owner contract that must not change (AC2), and quality-report handling
of non-unique sqlite3.IntegrityError failures (AC4). AC1/AC4 tests are RED
at the unfixed base and GREEN after the fix; the AC2 and unique-race legs
pin behaviour that must keep passing. Real temp SQLite DB, real app, the
user/policy/CSRF seams overridden as in test_1153_800_guardrails.py; the
vault evaluator reads the seeded vault_members rows so the
404 -> vault-403 -> owner-403 ordering is real.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,  # noqa: E402
)
from app.api.routes import chat as chat_module  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,  # noqa: E402
    init_db,
    run_migrations,
)
from app.security import csrf_protect  # noqa: E402

ADMIN = {"id": 1, "username": "admin1", "role": "admin", "is_active": True}
OWNER = {"id": 3, "username": "member1", "role": "member", "is_active": True}
INTRUDER = {"id": 4, "username": "member2", "role": "member", "is_active": True}
OUTSIDER = {"id": 5, "username": "member3", "role": "member", "is_active": True}
COWORKER = {"id": 6, "username": "member4", "role": "member", "is_active": True}
VAULT, VAULT_OTHER = 101, 102
_ORDER = {"read": 0, "write": 1, "admin": 2}
# (surface, http method, path suffix, json body) for the six mutating
# single-session surfaces (the stream boundary is driven directly).
SURFACES = (
    ("add_message", "post", "/messages", {"role": "user", "content": "x"}),
    (
        "add_messages_batch",
        "post",
        "/messages/batch",
        {"messages": [{"role": "user", "content": "x"}]},
    ),
    ("truncate", "post", "/truncate", {"keep_count": 0}),
    ("rename", "put", "", {"title": "renamed"}),
    ("delete", "delete", "", None),
)


class SharedVaultChatBase(unittest.TestCase):
    """A shared vault with several members; sessions are owned via user_id."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp(prefix="zc1193-owner-")
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._pool = SimpleConnectionPool(self._db_path)
        self._checker = sqlite3.connect(self._db_path, check_same_thread=False)
        self._current_user = OWNER
        conn = self._pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executemany(
                "INSERT OR IGNORE INTO users (id, username, hashed_password,"
                " role, is_active) VALUES (?, ?, 'x', ?, 1)",
                [
                    (1, "admin1", "admin"),
                    (3, "member1", "member"),
                    (4, "member2", "member"),
                    (5, "member3", "member"),
                    (6, "member4", "member"),
                ],
            )
            conn.executemany(
                "INSERT INTO vaults (id, name, description) VALUES (?, ?, 'v')",
                [(VAULT, f"vault-{VAULT}"), (VAULT_OTHER, f"vault-{VAULT_OTHER}")],
            )
            # OWNER, INTRUDER and COWORKER hold write on the shared VAULT;
            # only OWNER may touch VAULT_OTHER.
            conn.executemany(
                "INSERT INTO vault_members (vault_id, user_id, permission)"
                " VALUES (?, ?, 'write')",
                [(VAULT, 3), (VAULT, 4), (VAULT, 6), (VAULT_OTHER, 3)],
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)

        async def evaluate(principal, resource_type, resource_id, action):
            if principal.get("role") in ("admin", "superadmin"):
                return True
            row = self._checker.execute(
                "SELECT permission FROM vault_members WHERE vault_id = ?"
                " AND user_id = ?",
                (resource_id, principal["id"]),
            ).fetchone()
            return row is not None and _ORDER.get(row[0], -1) >= _ORDER.get(action, 99)

        def _override_get_db():
            db_conn = self._pool.get_connection()
            try:
                yield db_conn
            finally:
                self._pool.release_connection(db_conn)

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_active_user] = lambda: self._current_user
        app.dependency_overrides[get_evaluate_policy] = lambda: evaluate
        app.dependency_overrides[get_rag_engine] = lambda: None
        app.dependency_overrides[csrf_protect] = lambda: "t"
        for dep in (
            get_db,
            get_current_active_user,
            get_evaluate_policy,
            get_rag_engine,
            csrf_protect,
        ):
            self.addCleanup(app.dependency_overrides.pop, dep, None)
        self.addCleanup(self._pool.close_all)
        self.addCleanup(self._checker.close)
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _as(self, user):
        self._current_user = user

    def _seed_session(self, owner_id, vault_id=VAULT, messages=3, title="Owned"):
        conn = self._pool.get_connection()
        try:
            sid = conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title,"
                " created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP,"
                " CURRENT_TIMESTAMP)",
                (vault_id, owner_id, title),
            ).lastrowid
            for i in range(messages):
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content,"
                    " seq, created_at) VALUES (?, 'assistant', ?, ?,"
                    " CURRENT_TIMESTAMP)",
                    (sid, f"owner message {i}", i + 1),
                )
            conn.commit()
            return sid
        finally:
            self._pool.release_connection(conn)

    def _state(self, sid):
        conn = self._pool.get_connection()
        try:
            row = conn.execute(
                "SELECT title, user_id FROM chat_sessions WHERE id = ?", (sid,)
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            return {
                "exists": row is not None,
                "title": row[0] if row else None,
                "messages": count,
            }
        finally:
            self._pool.release_connection(conn)

    def _run_stream_auth(self, body):
        """Drive get_stream_auth directly on a real pooled connection (the
        _run_auth recipe from test_1153_800_guardrails.py)."""
        pool = SQLiteConnectionPool(self._db_path, max_size=2)
        had_pool = hasattr(app.state, "db_pool")
        prev_pool = getattr(app.state, "db_pool", None)
        app.state.db_pool = pool

        async def _resolve(conn, request, authorization, access_token):
            return self._current_user

        original = chat_module._resolve_active_user
        chat_module._resolve_active_user = _resolve

        async def _go():
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/chat/stream",
                "headers": [],
                "query_string": b"",
            }
            request = Request(scope)
            request.scope["app"] = app
            return await chat_module.get_stream_auth(request, body)

        try:
            return asyncio.run(_go())
        finally:
            chat_module._resolve_active_user = original
            if had_pool:
                app.state.db_pool = prev_pool
            else:
                delattr(app.state, "db_pool")
            pool.close_all()

    def _stream_body(self, sid):
        return chat_module.ChatStreamRequest(
            messages=[{"role": "user", "content": "hello"}],
            vault_id=VAULT,
            session_id=sid,
            turn_id="probe-turn-1",
        )

    def _request(self, surface, sid):
        """One mutating HTTP request against `surface` of session `sid`."""
        for name, method, suffix, body in SURFACES:
            if name == surface:
                path = f"/api/chat/sessions/{sid}{suffix}"
                if body is None:  # httpx delete() takes no json kwarg
                    return getattr(self.client, method)(path)
                return getattr(self.client, method)(path, json=body)
        raise ValueError(f"unknown surface {surface}")


class ChatSessionOwnerScopeTest(SharedVaultChatBase):
    """AC1: a non-owner, non-admin vault writer is denied with 403 and the
    database is untouched, on every mutating single-session surface."""

    def test_non_owner_denied_on_every_http_mutation_surface(self):
        # Each surface gets a fresh session seeded with seq-ordered messages,
        # so a passed gate would observably change the database.
        for surface, _method, _suffix, _body in SURFACES:
            with self.subTest(surface=surface):
                sid = self._seed_session(OWNER["id"])
                before = self._state(sid)
                self._as(INTRUDER)
                r = self._request(surface, sid)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn("another user", r.json()["detail"])
                self.assertEqual(
                    self._state(sid), before, f"{surface} changed the database"
                )

    def test_non_owner_stream_auth_denied(self):
        sid = self._seed_session(OWNER["id"])
        self._as(INTRUDER)
        with self.assertRaises(HTTPException) as ctx:
            self._run_stream_auth(self._stream_body(sid))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("another user", str(ctx.exception.detail))

    def test_owner_and_admin_positive_controls(self):
        # Positive controls that must hold after the fix too.
        sid = self._seed_session(OWNER["id"])
        self._as(OWNER)
        added = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "user", "content": "mine"},
        )
        renamed = self.client.put(
            f"/api/chat/sessions/{sid}", json={"title": "renamed by owner"}
        )
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(self._state(sid)["title"], "renamed by owner")
        self._as(ADMIN)
        truncated = self.client.post(
            f"/api/chat/sessions/{sid}/truncate", json={"keep_count": 0}
        )
        deleted = self.client.delete(f"/api/chat/sessions/{sid}")
        self.assertEqual(truncated.status_code, 200, truncated.text)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(self._state(sid)["exists"])

    def test_authorization_ordering_vault_404_before_owner_403(self):
        # An outsider gets the VAULT 403 (and so does an intruder facing a
        # vault they cannot access at all), and a missing session stays 404;
        # none of these may surface the owner denial.
        sid = self._seed_session(OWNER["id"])
        self._as(OUTSIDER)
        r = self.client.delete(f"/api/chat/sessions/{sid}")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("No write access", r.json()["detail"])
        self.assertNotIn("another user", r.json()["detail"])
        self.assertTrue(self._state(sid)["exists"])
        other = self._seed_session(OWNER["id"], vault_id=VAULT_OTHER)
        self._as(INTRUDER)
        unreachable = self.client.delete(f"/api/chat/sessions/{other}")
        self.assertEqual(unreachable.status_code, 403, unreachable.text)
        self.assertIn("No write access", unreachable.json()["detail"])
        self.assertTrue(self._state(other)["exists"])
        missing = self.client.delete("/api/chat/sessions/999999")
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertIn("Session not found", missing.json()["detail"])

    def test_sequential_id_enumeration_hits_no_neighbors(self):
        # Adversarial companion: session ids are sequential, so the denial
        # must also hold for a neighbour owned by yet another user.
        first = self._seed_session(OWNER["id"])
        second = self._seed_session(COWORKER["id"])
        self.assertEqual(second, first + 1, "fixture must produce adjacent ids")
        self._as(INTRUDER)
        for sid in (first, second):
            r = self.client.delete(f"/api/chat/sessions/{sid}")
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("another user", r.json()["detail"])
            self.assertTrue(self._state(sid)["exists"])


class LegacySharedSessionTest(SharedVaultChatBase):
    """AC2: legacy user_id IS NULL sessions stay mutable by any vault writer."""

    def test_null_owner_session_stays_mutable_by_vault_writer(self):
        sid = self._seed_session(None, messages=2)
        self._as(INTRUDER)
        added = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "user", "content": "legacy write"},
        )
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(self._state(sid)["messages"], 3)
        renamed = self.client.put(
            f"/api/chat/sessions/{sid}", json={"title": "renamed on legacy"}
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(self._state(sid)["title"], "renamed on legacy")
        truncated = self.client.post(
            f"/api/chat/sessions/{sid}/truncate", json={"keep_count": 0}
        )
        self.assertEqual(truncated.status_code, 200, truncated.text)
        self.assertEqual(self._state(sid)["messages"], 0)
        deleted = self.client.delete(f"/api/chat/sessions/{sid}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(self._state(sid)["exists"])

    def test_null_owner_session_stream_auth_does_not_raise(self):
        sid = self._seed_session(None, messages=1)
        self._as(INTRUDER)
        user = self._run_stream_auth(self._stream_body(sid))
        self.assertEqual(user["id"], INTRUDER["id"])


class _ShadowCursor:
    """Cursor proxy whose result set is hidden (fetchone/fetchall empty)."""

    def __init__(self, real):
        self._real = real

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __getattr__(self, name):
        return getattr(self._real, name)


class _ConnProxy:
    """Shared connection-proxy plumbing: delegate everything but execute."""

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _ParentVanisherProxy(_ConnProxy):
    """Deletes the parent row on a SECOND connection just before the armed
    INSERT runs, so the delegated INSERT fails the foreign key (the
    probe_f12.py / probe_convert_fk.py recipe)."""

    def __init__(self, real, db_path, insert_prefix, delete_sql, param_index):
        self._real, self._db_path = real, db_path
        self._insert_prefix, self._delete_sql = insert_prefix, delete_sql
        self._param_index = param_index
        self.fired = False

    def execute(self, sql, params=()):
        if sql.startswith(self._insert_prefix) and not self.fired:
            self.fired = True
            other = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
            other.execute("PRAGMA foreign_keys = ON")
            other.execute(self._delete_sql, (params[self._param_index],))
            other.commit()
            other.close()
        return self._real.execute(sql, params)


class _UniqueRaceProxy(_ConnProxy):
    """One-shot unique-race simulator (the _RaceSimConnection pattern from
    test_p04_misc_routes.py): shadows the first SELECT reading the table and
    raises sqlite3.IntegrityError on the first INSERT into it."""

    def __init__(self, real, table):
        self._real, self._table = real, table
        self.events = []
        self._shadow, self._raise = True, True

    def execute(self, sql, params=()):
        stripped = sql.lstrip()
        if (
            self._shadow
            and stripped.upper().startswith("SELECT")
            and f"FROM {self._table}" in sql
        ):
            self._shadow = False
            self.events.append("select-shadowed")
            return _ShadowCursor(self._real.execute(sql, params))
        if self._raise and stripped.startswith(f"INSERT INTO {self._table} "):
            self._raise = False
            self.events.append("insert-attempted")
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: simulated concurrent winner"
            )
        return self._real.execute(sql, params)


class QualityReportEdgeTest(unittest.TestCase):
    """AC4: a non-unique IntegrityError maps to the route's own 404 for the
    vanished parent, while the genuine unique race keeps returning the
    winner."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp(prefix="zc1193-qr-")
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        vault_id = self.conn.execute(
            "INSERT INTO vaults (name, description) VALUES ('qr', 'v')"
        ).lastrowid
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, 'QR', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (vault_id,),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id,"
            " seq, status, created_at) VALUES (?, 'user', 'question',"
            " 'turn-1', 1, 'done', CURRENT_TIMESTAMP)",
            (self.session_id,),
        )
        self.message_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id,"
            " seq, status, created_at) VALUES (?, 'assistant', 'answer',"
            " 'turn-1', 2, 'done', CURRENT_TIMESTAMP)",
            (self.session_id,),
        ).lastrowid
        self.conn.commit()

        async def allow(user, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_db] = lambda: self.conn
        app.dependency_overrides[get_current_active_user] = lambda: ADMIN
        app.dependency_overrides[get_evaluate_policy] = lambda: allow
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        for dep in (get_db, get_current_active_user, get_evaluate_policy, csrf_protect):
            self.addCleanup(app.dependency_overrides.pop, dep, None)
        from app.api.routes import quality_reports as _qr

        _qr._release_id_cache.clear()
        self.addCleanup(_qr._release_id_cache.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _report(self, category="incorrect_answer"):
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "category": category,
            "note": "n",
        }

    def test_submit_integrity_error_from_vanished_message_returns_404(self):
        proxy = _ParentVanisherProxy(
            self.conn,
            self._db_path,
            "INSERT INTO quality_reports",
            "DELETE FROM chat_messages WHERE id = ?",
            1,
        )
        app.dependency_overrides[get_db] = lambda: proxy
        r = self.client.post("/api/quality/reports", json=self._report())
        self.assertTrue(proxy.fired, "the racing INSERT never ran")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("Message not found in this session", r.text)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM quality_reports").fetchone()[0],
            0,
            "a report row survived the failed insert",
        )

    def test_convert_integrity_error_from_vanished_report_returns_404(self):
        report = self.client.post("/api/quality/reports", json=self._report()).json()
        proxy = _ParentVanisherProxy(
            self.conn,
            self._db_path,
            "INSERT INTO quality_eval_cases",
            "DELETE FROM quality_reports WHERE id = ?",
            0,
        )
        app.dependency_overrides[get_db] = lambda: proxy
        r = self.client.post(
            f"/api/quality/reports/{report['id']}/convert",
            json={"expected_outcome": "x"},
        )
        self.assertTrue(proxy.fired, "the racing INSERT never ran")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("Quality report not found", r.text)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM quality_eval_cases").fetchone()[0],
            0,
            "an eval case survived the failed insert",
        )

    def test_unique_race_integrity_error_still_returns_winner(self):
        # Preserving leg (#800 AC2): when the INSERT loses a genuine unique
        # race and a row with the same identity exists, the winner is
        # returned — never a 500, never a second row.
        winner_id = self.conn.execute(
            "INSERT INTO quality_reports (session_id, message_id, category,"
            " note, turn_id, seq, config_ref, source_file_hashes) VALUES (?, ?,"
            " 'stale_source', 'raced', 'turn-1', 2, 'unknown', '[]')",
            (self.session_id, self.message_id),
        ).lastrowid
        self.conn.commit()
        proxy = _UniqueRaceProxy(self.conn, "quality_reports")
        app.dependency_overrides[get_db] = lambda: proxy
        r = self.client.post(
            "/api/quality/reports", json=self._report(category="stale_source")
        )
        self.assertIn("select-shadowed", proxy.events, "the dedup pre-select never ran")
        self.assertIn("insert-attempted", proxy.events, "the racing INSERT never fired")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["id"], winner_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM quality_reports WHERE category = 'stale_source'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
