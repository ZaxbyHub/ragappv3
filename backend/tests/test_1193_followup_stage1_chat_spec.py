"""Supplementary specification tests for the PR #1193 follow-up Stage 1
(groups A and B of the collated #1201 + #1202 contract).

Group A (``controls``) pins the owner rule on every mutating chat-session
surface (member: own sessions only; admin/superadmin: any session; legacy
``user_id IS NULL`` sessions: every vault writer) and the shared check
ordering 404 -> vault 403 -> owner 403 -> cap 422.

Group B (``integrity_fallback``) pins the IntegrityError fallback principle
on the quality-report routes and the durable-turn write helpers: only a
UNIQUE message may take the benign unique-race recovery; any other integrity
failure maps to "the referenced parent is gone" only when a post-rollback
lookup finds it gone, and otherwise propagates. Real temp SQLite DB, real
app, the user/policy/CSRF seams overridden as in test_1153_800_guardrails.py.
"""

import asyncio
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
)
from app.api.routes import chat as chat_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.security import csrf_protect  # noqa: E402

ADMIN = {"id": 1, "username": "admin1", "role": "admin", "is_active": True}
OWNER = {"id": 3, "username": "member1", "role": "member", "is_active": True}
COWORKER = {"id": 4, "username": "member2", "role": "member", "is_active": True}
OUTSIDER = {"id": 5, "username": "member3", "role": "member", "is_active": True}
SUPERADMIN = {"id": 7, "username": "super1", "role": "superadmin", "is_active": True}
VAULT, VAULT_OTHER = 101, 102
_ORDER = {"read": 0, "write": 1, "admin": 2}
# (surface, http method, path suffix, json body) for the five mutating
# single-session HTTP surfaces (the stream boundary is driven directly).
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


class SharedVaultControlsBase(unittest.TestCase):
    """A shared vault with several members; sessions are owned via user_id.

    ADMIN and SUPERADMIN also hold write rows on the shared vault: the stream
    boundary (get_stream_auth) runs the real service-layer evaluator, which
    grants only superadmins a role bypass, so an admin needs the vault grant
    row to reach the positive stream control.
    """

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp(prefix="zc1193-stage1-controls-")
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
                    (7, "super1", "superadmin"),
                ],
            )
            conn.executemany(
                "INSERT INTO vaults (id, name, description) VALUES (?, ?, 'v')",
                [(VAULT, "v-101"), (VAULT_OTHER, "v-102")],
            )
            # OWNER (3), COWORKER (4), ADMIN (1) and SUPERADMIN (7) hold write
            # on the shared VAULT; only OWNER may touch VAULT_OTHER.
            conn.executemany(
                "INSERT INTO vault_members (vault_id, user_id, permission)"
                " VALUES (?, ?, 'write')",
                [(VAULT, 1), (VAULT, 3), (VAULT, 4), (VAULT, 7), (VAULT_OTHER, 3)],
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
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/chat/stream",
                    "headers": [],
                    "query_string": b"",
                }
            )
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

    def _assert_effect_visible(self, surface, sid):
        """The seeded session carries 3 messages and the title 'Owned'; the
        mutation's effect must be visible in the database."""
        state = self._state(sid)
        if surface == "delete":
            self.assertFalse(state["exists"], f"{surface} row not gone")
        elif surface == "rename":
            self.assertEqual(state["title"], "renamed", f"{surface} not applied")
        elif surface == "truncate":
            self.assertEqual(state["messages"], 0, f"{surface} rows not trimmed")
        else:  # add_message / add_messages_batch: one row added
            self.assertEqual(state["messages"], 4, f"{surface} row not added")


class OwnerRuleControlsTest(SharedVaultControlsBase):
    """Group A: the owner rule and cap-ordering controls."""

    def test_controls_owner_admin_superadmin_succeed_on_every_mutation_surface(self):
        # Positive controls (contract items A1/A2/A3): OWNER (the session's
        # non-admin owner), ADMIN and SUPERADMIN each succeed on every
        # mutating surface, with the effect visible in the database.
        for principal in (OWNER, ADMIN, SUPERADMIN):
            self._as(principal)
            for surface, _m, _s, _b in SURFACES:
                with self.subTest(role=principal["role"], surface=surface):
                    sid = self._seed_session(OWNER["id"])
                    r = self._request(surface, sid)
                    self.assertEqual(r.status_code, 200, r.text)
                    self._assert_effect_visible(surface, sid)

    def test_controls_stream_auth_positive_for_owner_admin_superadmin(self):
        sid = self._seed_session(OWNER["id"])
        for principal in (OWNER, ADMIN, SUPERADMIN):
            with self.subTest(role=principal["role"]):
                self._as(principal)
                user = self._run_stream_auth(self._stream_body(sid))
                self.assertEqual(user["id"], principal["id"])

    def test_controls_null_owner_session_stays_writable_and_streamable(self):
        sid = self._seed_session(None, messages=2)
        self._as(COWORKER)
        added = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={"messages": [{"role": "user", "content": "legacy batch write"}]},
        )
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(self._state(sid)["messages"], 3)
        user = self._run_stream_auth(self._stream_body(sid))
        self.assertEqual(user["id"], COWORKER["id"])

    def test_controls_exact_cap_user_item_accepted_on_batch_route(self):
        cap = settings.chat_message_max_chars
        sid = self._seed_session(OWNER["id"])
        self._as(OWNER)
        r = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={"messages": [{"role": "user", "content": "q" * cap}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._state(sid)["messages"], 4)

    def _post_over_cap(self, sid, route):
        cap = settings.chat_message_max_chars
        item = {"role": "user", "content": "Q" * (cap + 1)}
        body = item if route == "messages" else {"messages": [item]}
        if route == "messages/batch":
            body["messages"].append({"role": "user", "content": "valid sibling"})
        return self.client.post(f"/api/chat/sessions/{sid}/{route}", json=body)

    def test_controls_owner_denial_precedes_cap_on_single_route(self):
        # A coworker's over-cap user item aimed at another member's owned
        # session meets the owner denial, never the cap validation, and
        # writes nothing.
        sid = self._seed_session(OWNER["id"])
        before = self._state(sid)
        self._as(COWORKER)
        r = self._post_over_cap(sid, "messages")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("another user", r.json()["detail"])
        self.assertNotEqual(r.status_code, 422, "the cap check must not fire first")
        self.assertEqual(self._state(sid), before, "the denied request wrote rows")

    def test_controls_owner_denial_precedes_cap_on_batch_route(self):
        # A batch whose first item is an over-cap user item aimed at another
        # member's owned session meets the owner denial, never the cap
        # validation, and writes nothing.
        sid = self._seed_session(OWNER["id"])
        before = self._state(sid)
        self._as(COWORKER)
        r = self._post_over_cap(sid, "messages/batch")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("another user", r.json()["detail"])
        self.assertNotEqual(r.status_code, 422, "the cap check must not fire first")
        self.assertEqual(self._state(sid), before, "the denied request wrote rows")

    def test_controls_vault_and_404_denials_precede_cap(self):
        # An over-cap user item aimed at a session in a vault the caller
        # cannot write meets the vault denial, and one aimed at a missing
        # session meets the 404 — on both write routes, never 422.
        other = self._seed_session(OWNER["id"], vault_id=VAULT_OTHER)
        for caller in (COWORKER, OUTSIDER):
            self._as(caller)
            for route in ("messages", "messages/batch"):
                with self.subTest(
                    route=route, case="no-vault-access", caller=caller["id"]
                ):
                    r = self._post_over_cap(other, route)
                    self.assertEqual(r.status_code, 403, r.text)
                    self.assertIn("No write access", r.json()["detail"])
                    self.assertNotEqual(
                        r.status_code, 422, "the cap check must not fire first"
                    )
        self._as(COWORKER)
        for route in ("messages", "messages/batch"):
            with self.subTest(route=route, case="missing-session"):
                missing = self._post_over_cap(999999, route)
                self.assertEqual(missing.status_code, 404, missing.text)
                self.assertIn("Session not found", missing.json()["detail"])
                self.assertNotEqual(
                    missing.status_code, 422, "the cap check must not fire first"
                )
        self.assertEqual(
            self._state(other)["messages"], 3, "the denied requests wrote rows"
        )


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
    probe_f12.py recipe)."""

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


class _InterposerProxy(_ConnProxy):
    """One-shot statement interposer with two independent arms: the FIRST
    SELECT reading `table` returns an empty result set (the caller's own
    existence lookup misses), and the first INSERT into `table` raises
    sqlite3.IntegrityError with `insert_error` (a non-unique integrity
    failure). Each arm fires at most once; each is optional."""

    def __init__(self, real, table, insert_error=None, shadow_select=True):
        self._real, self._table = real, table
        self._insert_error = insert_error
        self.shadowed = False
        self.fired = False
        self.events = []
        self._shadow = shadow_select
        self._insert_armed = insert_error is not None

    def execute(self, sql, params=()):
        stripped = sql.lstrip()
        if (
            self._shadow
            and stripped.upper().startswith("SELECT")
            and f"FROM {self._table}" in sql
        ):
            self._shadow = False
            self.shadowed = True
            self.events.append("select-shadowed")
            return _ShadowCursor(self._real.execute(sql, params))
        if self._insert_armed and stripped.startswith(f"INSERT INTO {self._table} "):
            self._insert_armed = False
            self.fired = True
            self.events.append("insert-attempted")
            raise sqlite3.IntegrityError(self._insert_error)
        return self._real.execute(sql, params)


class _OneShotPool:
    """db_pool stand-in handing the pre-write one wrapped connection."""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return contextlib.nullcontext(self._conn)


def _upsert_payload(session_id, turn_id, status, content):
    """A full _upsert_assistant_turn payload for one durable turn."""
    payload = {
        key: None
        for key in (
            "sources",
            "memories",
            "wiki_refs",
            "kms_refs",
            "citation_confidence",
            "unverifiable_claims",
            "currency_warnings",
            "citation_enforcement",
        )
    }
    payload.update(session_id=session_id, turn_id=turn_id, status=status)
    payload["content"] = content
    return payload


class IntegrityFallbackTest(unittest.TestCase):
    """Group B: IntegrityError fallbacks on the quality-report routes and
    the durable-turn write helpers."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp(prefix="zc1193-stage1-qr-")
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.vault_id = self.conn.execute(
            "INSERT INTO vaults (name, description) VALUES ('qr', 'v')"
        ).lastrowid
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, 'QR', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (self.vault_id,),
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

    def _seed_session(self, title):
        sid = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, ?, CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (self.vault_id, title),
        ).lastrowid
        self.conn.commit()
        return sid

    def test_integrity_fallback_submit_vanished_session_404(self):
        # The SESSION row disappears between the handler's own lookups and
        # its INSERT: the response is the route's own missing-session 404 and
        # no report row is written.
        proxy = _ParentVanisherProxy(
            self.conn,
            self._db_path,
            "INSERT INTO quality_reports",
            "DELETE FROM chat_sessions WHERE id = ?",
            0,
        )
        app.dependency_overrides[get_db] = lambda: proxy
        r = self.client.post("/api/quality/reports", json=self._report())
        self.assertTrue(proxy.fired, "the racing INSERT never ran")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("Session not found", r.text)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM quality_reports").fetchone()[0],
            0,
            "a report row survived the failed insert",
        )

    def test_integrity_fallback_submit_nonunique_error_not_mapped_to_missing_parent(
        self,
    ):
        # A non-unique IntegrityError while BOTH the session row and the
        # message row still exist: the response must never claim a parent is
        # missing (any other outcome is acceptable).
        proxy = _InterposerProxy(
            self.conn,
            "quality_reports",
            insert_error="CHECK constraint failed: simulated",
            shadow_select=False,
        )
        app.dependency_overrides[get_db] = lambda: proxy
        r = self.client.post("/api/quality/reports", json=self._report())
        self.assertTrue(proxy.fired, "the simulated INSERT never ran")
        if r.status_code == 404:
            self.assertNotIn("Message not found in this session", r.text)
            self.assertNotIn("Session not found", r.text)
        # A non-unique failure must not be swallowed: the ORIGINAL error
        # propagates out of the handler (the winner-recovery path cannot).
        # Use a FRESH proxy: the first request consumed the one-shot error.
        strict_proxy = _InterposerProxy(
            self.conn,
            "quality_reports",
            insert_error="CHECK constraint failed: simulated",
            shadow_select=False,
        )
        app.dependency_overrides[get_db] = lambda: strict_proxy
        strict = TestClient(app, raise_server_exceptions=True)
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            strict.post("/api/quality/reports", json=self._report())
        self.assertTrue(strict_proxy.fired, "the strict INSERT never ran")
        self.assertIn("CHECK constraint failed", str(caught.exception))

    def test_integrity_fallback_convert_unique_race_returns_winner(self):
        # The unique-index race on convert: when the INSERT loses to a
        # concurrent winner, the winner's existing case id is returned.
        report = self.client.post("/api/quality/reports", json=self._report()).json()
        winner_id = self.conn.execute(
            "INSERT INTO quality_eval_cases (report_id, query,"
            " expected_outcome, provenance) VALUES (?, 'winner query', 'x', '{}')",
            (report["id"],),
        ).lastrowid
        self.conn.commit()
        proxy = _InterposerProxy(
            self.conn,
            "quality_eval_cases",
            insert_error="UNIQUE constraint failed: simulated concurrent winner",
        )
        app.dependency_overrides[get_db] = lambda: proxy
        r = self.client.post(
            f"/api/quality/reports/{report['id']}/convert",
            json={"expected_outcome": "x"},
        )
        self.assertIn("select-shadowed", proxy.events, "the dedup pre-select never ran")
        self.assertIn("insert-attempted", proxy.events, "the racing INSERT never fired")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["id"], winner_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM quality_eval_cases WHERE report_id = ?",
                (report["id"],),
            ).fetchone()[0],
            1,
        )

    def test_integrity_fallback_convert_nonunique_error_not_mapped_to_missing_report(
        self,
    ):
        # A non-unique IntegrityError while the report row still exists: the
        # response must never claim the report is missing.
        report = self.client.post("/api/quality/reports", json=self._report()).json()
        proxy = _InterposerProxy(
            self.conn,
            "quality_eval_cases",
            insert_error="CHECK constraint failed: simulated",
            shadow_select=False,
        )
        app.dependency_overrides[get_db] = lambda: proxy
        r = self.client.post(
            f"/api/quality/reports/{report['id']}/convert",
            json={"expected_outcome": "x"},
        )
        self.assertTrue(proxy.fired, "the simulated INSERT never ran")
        if r.status_code == 404:
            self.assertNotIn("Quality report not found", r.text)
        # The exact original exception must escape, not be mapped to a
        # misleading not-found or swallowed into a success response.
        # Fresh proxy: the first request consumed the one-shot error.
        strict_proxy = _InterposerProxy(
            self.conn,
            "quality_eval_cases",
            insert_error="CHECK constraint failed: simulated",
            shadow_select=False,
        )
        app.dependency_overrides[get_db] = lambda: strict_proxy
        strict = TestClient(app, raise_server_exceptions=True)
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            strict.post(
                f"/api/quality/reports/{report['id']}/convert",
                json={"expected_outcome": "x"},
            )
        self.assertTrue(strict_proxy.fired, "the strict INSERT never ran")
        self.assertIn("CHECK constraint failed", str(caught.exception))

    def test_integrity_fallback_prewrite_nonunique_error_is_failed_not_duplicate(self):
        # The session disappears between _prewrite_user_turn's own lookup and
        # its INSERT (the foreign key fails): a FAILED pre-write — ok False,
        # duplicate False — never a duplicate, and the function must not
        # raise.
        sid = self._seed_session("PW")
        pool_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        pool_conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(pool_conn.close)
        proxy = _ParentVanisherProxy(
            pool_conn,
            self._db_path,
            "INSERT INTO chat_messages",
            "DELETE FROM chat_sessions WHERE id = ?",
            0,
        )
        result = chat_module._prewrite_user_turn(
            _OneShotPool(proxy), sid, "turn-b5", "q"
        )
        self.assertTrue(proxy.fired, "the pre-write INSERT never ran")
        self.assertIs(result["ok"], False, f"pre-write result: {result}")
        self.assertIs(result["duplicate"], False, f"pre-write result: {result}")

    def test_integrity_fallback_prewrite_genuine_duplicate_is_ok_and_duplicate(self):
        # A genuine duplicate (the turn's user row already exists) is the
        # benign unique-race: ok True, duplicate True.
        sid = self._seed_session("PW2")
        pool = SQLiteConnectionPool(self._db_path, max_size=2)
        self.addCleanup(pool.close_all)
        first = chat_module._prewrite_user_turn(pool, sid, "turn-b6", "first delivery")
        second = chat_module._prewrite_user_turn(pool, sid, "turn-b6", "redelivered")
        self.assertTrue(first["ok"], f"first pre-write result: {first}")
        self.assertFalse(first["duplicate"], f"first pre-write result: {first}")
        self.assertTrue(second["ok"], f"second pre-write result: {second}")
        self.assertTrue(second["duplicate"], f"second pre-write result: {second}")

    def test_integrity_fallback_upsert_assistant_turn_unique_race_and_propagation(self):
        # (a) A UNIQUE violation while a winner row exists: the winner row is
        # adopted and updated in place, nothing is raised. (b) A non-unique
        # IntegrityError with no existing row: the original error propagates.
        sid = self._seed_session("UP")
        winner_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id,"
            " seq, status, created_at) VALUES (?, 'assistant', 'winner',"
            " 'turn-b7a', 1, 'complete', CURRENT_TIMESTAMP)",
            (sid,),
        ).lastrowid
        self.conn.commit()
        shadow = _InterposerProxy(self.conn, "chat_messages")
        chat_module._upsert_assistant_turn(
            shadow, _upsert_payload(sid, "turn-b7a", "interrupted", "revised answer")
        )
        self.conn.commit()
        self.assertTrue(shadow.shadowed, "the existence lookup was never hidden")
        rows = self.conn.execute(
            "SELECT id, content, status FROM chat_messages WHERE session_id = ?"
            " AND turn_id = 'turn-b7a' AND role = 'assistant'",
            (sid,),
        ).fetchall()
        self.assertEqual(len(rows), 1, "the race recovery duplicated the row")
        self.assertEqual(rows[0][0], winner_id, "the winner row id changed")
        self.assertEqual(rows[0][1], "revised answer")
        self.assertEqual(rows[0][2], "interrupted")

        raising = _InterposerProxy(
            self.conn,
            "chat_messages",
            insert_error="CHECK constraint failed: simulated",
            shadow_select=False,
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            chat_module._upsert_assistant_turn(
                raising, _upsert_payload(sid, "turn-b7b", "complete", "orphan")
            )
        self.assertIn("CHECK constraint failed: simulated", str(ctx.exception))
        self.assertTrue(raising.fired, "the simulated INSERT never ran")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?"
                " AND turn_id = 'turn-b7b'",
                (sid,),
            ).fetchone()[0],
            0,
            "a row survived the failed insert",
        )


if __name__ == "__main__":
    unittest.main()
