"""Frozen specification tests for the PR #1193 follow-up (#1202 side).

Sanitizer event-loop dispatch (AC5: AST census + thread-identity evidence on
the write routes, the non-stream route and the stream done path), the
chat_message_max_chars cap on the write routes (AC6), quoted-fence
preservation (AC7), and re-sanitize stability when a wrapper tag shares a
line with a fence delimiter (AC8). AC5-AC8 behaviour tests are RED at the
unfixed base and GREEN after the fix; the #922 contract legs and the
closer-line stability guard pin behaviour that must keep passing.
"""

import ast
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
    get_vector_store,
)
from app.api.routes import chat as chat_module  # noqa: E402
from app.api.routes.chat import get_stream_auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402
from app.security import csrf_protect  # noqa: E402
from app.utils.assistant_sanitizer import sanitize_assistant_content  # noqa: E402

CHAT_PY = Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "chat.py"

MEMBER = {"id": 3, "username": "member1", "role": "member", "is_active": True}
OUTSIDER = {"id": 5, "username": "member3", "role": "member", "is_active": True}
VAULT = 201
_ORDER = {"read": 0, "write": 1, "admin": 2}


def _callee_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _chat_tree():
    return ast.parse(CHAT_PY.read_text(encoding="utf-8"))


def _sanitizer_names(tree):
    names = {"sanitize_assistant_content"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.endswith("assistant_sanitizer")
        ):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _to_thread_wrapped_call_ids(tree):
    """ids of Call nodes that are the first positional arg of asyncio.to_thread."""
    return {
        id(node.args[0])
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and _callee_name(node) == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Call)
        )
    }


def _own_call_names(func_node):
    """Callee names of calls owned by the function itself, excluding anything
    inside nested function/lambda definitions."""
    nested_ids = set()
    for child in ast.walk(func_node):
        if child is not func_node and isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            for desc in ast.walk(child):
                nested_ids.add(id(desc))
    return {
        _callee_name(call)
        for call in ast.walk(func_node)
        if isinstance(call, ast.Call) and id(call) not in nested_ids
    }


def _on_loop_sync_helpers(tree, sanitizer_names):
    """Sync functions reaching a sanitizer call directly or through other
    sync functions of the same module (transitive closure)."""
    sync_calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            sync_calls.setdefault(node.name, set()).update(_own_call_names(node))
    helpers = set()
    changed = True
    while changed:
        changed = False
        for name, callees in sync_calls.items():
            if name not in helpers and (callees & sanitizer_names or callees & helpers):
                helpers.add(name)
                changed = True
    return helpers


def _async_findings(tree, sanitizer_names, helpers):
    """(unwrapped sanitizer calls, unwrapped helper calls) inside async defs;
    each helper entry is (lineno, name, sits_inside_a_finally_block)."""
    wrapped = _to_thread_wrapped_call_ids(tree)
    census, direct = [], []
    for af in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
        finally_ids = set()
        for node in ast.walk(af):
            if isinstance(node, ast.Try):
                for stmt in node.finalbody:
                    for desc in ast.walk(stmt):
                        finally_ids.add(id(desc))
        for node in ast.walk(af):
            if not isinstance(node, ast.Call) or id(node) in wrapped:
                continue
            name = _callee_name(node)
            if name in sanitizer_names:
                census.append(f"line {node.lineno}: direct {name}() in async def")
            elif name in helpers:
                direct.append((node.lineno, name, id(node) in finally_ids))
    return census, direct


class SanitizerDispatchCensusTest(unittest.TestCase):
    """AC5 (structural half): sanitizer calls reachable from an async handler
    go through asyncio.to_thread; the only tolerated direct helper call is
    the cancellation backstop inside a finally block."""

    def test_no_sanitizer_call_inside_async_defs(self):
        tree = _chat_tree()
        census, _ = _async_findings(tree, _sanitizer_names(tree), set())
        self.assertEqual(census, [], "sanitizer called synchronously on the loop")

    def test_sync_sanitizer_helpers_reached_only_via_to_thread(self):
        tree = _chat_tree()
        names = _sanitizer_names(tree)
        helpers = _on_loop_sync_helpers(tree, names)
        # Guard against a vacuous pass: the known on-loop helpers must be in
        # the computed set, or the transitive analysis itself is broken.
        for known in (
            "_upsert_assistant_turn",
            "_canonical_done_content",
            "_finalize_durable_turn_sync",
        ):
            self.assertIn(known, helpers)
        _, direct = _async_findings(tree, names, helpers)
        outside_finally = [
            f"line {lineno}: direct {name}() outside a finally block"
            for lineno, name, in_finally in direct
            if not in_finally
        ]
        self.assertEqual(
            outside_finally, [], "on-loop helper called directly from an async handler"
        )
        self.assertLessEqual(
            len(direct), 1, "more than one tolerated finally-block backstop"
        )


class ChatWriteRouteBase(unittest.TestCase):
    """Real temp DB + real app; the evaluator and the mock engine record the
    event-loop thread id so tests can prove the sanitizer left the loop."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp(prefix="zc1193-offloop-")
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._pool = SimpleConnectionPool(self._db_path)
        self._checker = sqlite3.connect(self._db_path, check_same_thread=False)
        self._current_user = MEMBER
        self._loop_thread_ids = []
        self._sanitizer_threads = []
        conn = self._pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            for uid, name in ((3, "member1"), (5, "member3")):
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, username,"
                    " hashed_password, role, is_active)"
                    " VALUES (?, ?, 'x', 'member', 1)",
                    (uid, name),
                )
            conn.execute(
                "INSERT INTO vaults (id, name, description) VALUES (?, ?, 'v')",
                (VAULT, f"vault-{VAULT}"),
            )
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id,"
                " permission) VALUES (?, 3, 'write')",
                (VAULT,),
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)

        async def evaluate(
            principal,
            resource_type=None,
            resource_id=None,
            action=None,
            *args,
            **kwargs,
        ):
            self._loop_thread_ids.append(threading.get_ident())
            if principal.get("role") in ("admin", "superadmin"):
                return True
            row = self._checker.execute(
                "SELECT permission FROM vault_members WHERE vault_id = ?"
                " AND user_id = ?",
                (resource_id, principal["id"]),
            ).fetchone()
            return row is not None and _ORDER.get(row[0], -1) >= _ORDER.get(action, 99)

        async def engine_query(*args, **kwargs):
            self._loop_thread_ids.append(threading.get_ident())
            yield {"type": "content", "content": "<answer>hi</answer>"}
            yield {"type": "done", "sources": [], "memories_used": []}

        engine = MagicMock()
        engine.llm_client = None
        engine.query = engine_query
        ready_vs = MagicMock()
        ready_vs._ready = True

        def _override_get_db():
            db_conn = self._pool.get_connection()
            try:
                yield db_conn
            finally:
                self._pool.release_connection(db_conn)

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_active_user] = lambda: self._current_user
        app.dependency_overrides[get_evaluate_policy] = lambda: evaluate
        app.dependency_overrides[get_rag_engine] = lambda: engine
        app.dependency_overrides[get_vector_store] = lambda: ready_vs
        app.dependency_overrides[csrf_protect] = lambda: "t"
        for dep in (
            get_db,
            get_current_active_user,
            get_evaluate_policy,
            get_rag_engine,
            get_vector_store,
            csrf_protect,
        ):
            self.addCleanup(app.dependency_overrides.pop, dep, None)
        self.addCleanup(self._pool.close_all)
        self.addCleanup(self._checker.close)
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _as(self, user):
        self._current_user = user

    def _seed_session(self, messages=2):
        conn = self._pool.get_connection()
        try:
            sid = conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title,"
                " created_at, updated_at) VALUES (?, 3, 'S', CURRENT_TIMESTAMP,"
                " CURRENT_TIMESTAMP)",
                (VAULT,),
            ).lastrowid
            for i in range(messages):
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content,"
                    " seq, created_at) VALUES (?, 'user', ?, ?,"
                    " CURRENT_TIMESTAMP)",
                    (sid, f"message {i}", i + 1),
                )
            conn.commit()
            return sid
        finally:
            self._pool.release_connection(conn)

    def _message_count(self, sid):
        conn = self._pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        finally:
            self._pool.release_connection(conn)

    def _record_sanitizer_thread(self):
        original = chat_module.sanitize_chat_messages_content
        threads = self._sanitizer_threads

        def recording(content):
            threads.append(threading.get_ident())
            return original(content)

        chat_module.sanitize_chat_messages_content = recording
        self.addCleanup(
            setattr, chat_module, "sanitize_chat_messages_content", original
        )

    def _assert_sanitized_off_loop(self):
        self.assertTrue(self._sanitizer_threads, "the sanitizer never ran")
        self.assertTrue(self._loop_thread_ids, "no async path captured the loop thread")
        loop_thread = self._loop_thread_ids[-1]
        for thread_id in self._sanitizer_threads:
            self.assertNotEqual(
                thread_id, loop_thread, "sanitizer ran on the event-loop thread"
            )


class SanitizerOffLoopBehaviorTest(ChatWriteRouteBase):
    """AC5 (behavioural half): the sanitizer runs on a worker thread, not the
    event loop, on the write routes and the non-stream/stream chat paths."""

    def test_write_routes_sanitize_off_the_event_loop(self):
        self._record_sanitizer_thread()
        for route in ("messages", "messages/batch"):
            with self.subTest(route=route):
                sid = self._seed_session()
                body = (
                    {"role": "assistant", "content": "<answer>hi</answer>"}
                    if route == "messages"
                    else {
                        "messages": [
                            {"role": "assistant", "content": "<answer>hi</answer>"}
                        ]
                    }
                )
                r = self.client.post(f"/api/chat/sessions/{sid}/{route}", json=body)
                self.assertEqual(r.status_code, 200, r.text)
                payload = r.json()
                content = (
                    payload["content"]
                    if route == "messages"
                    else payload["messages"][0]["content"]
                )
                self.assertEqual(content, "hi")
                self._assert_sanitized_off_loop()

    def test_nonstream_chat_sanitizes_off_the_event_loop(self):
        self._record_sanitizer_thread()
        r = self.client.post("/api/chat", json={"message": "hello", "vault_id": VAULT})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["content"], "hi")
        self._assert_sanitized_off_loop()

    def test_stream_done_sanitizes_off_the_event_loop(self):
        self._record_sanitizer_thread()
        app.dependency_overrides[get_stream_auth] = lambda: dict(MEMBER)
        self.addCleanup(app.dependency_overrides.pop, get_stream_auth, None)
        done_lines = []
        with self.client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": VAULT,
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            for line in response.iter_lines():
                if "repaired_content" in line:
                    done_lines.append(line)
        self.assertTrue(done_lines, "the stream never emitted a done payload")
        self.assertIn('"hi"', done_lines[-1])
        self._assert_sanitized_off_loop()


class ChatWriteCapTest(ChatWriteRouteBase):
    """AC6: user-role items over chat_message_max_chars get the stream route's
    422 on both write routes; the #922 contract legs stay green."""

    def test_max_chars_over_cap_user_item_422_on_both_write_routes(self):
        # The single route rejects the over-cap user item; the batch route
        # rejects the WHOLE batch (all-or-nothing — nothing written). The
        # detail is a plain string with the stream route's message tail and
        # never echoes the content.
        cap = settings.chat_message_max_chars
        over_cap = [{"role": "user", "content": "Q" * (cap + 1)}]
        for route in ("messages", "messages/batch"):
            with self.subTest(route=route):
                sid = self._seed_session()
                body = (
                    over_cap[0]
                    if route == "messages"
                    else {
                        "messages": over_cap
                        + [{"role": "user", "content": "valid sibling"}]
                    }
                )
                r = self.client.post(f"/api/chat/sessions/{sid}/{route}", json=body)
                self.assertEqual(r.status_code, 422, r.text)
                detail = r.json()["detail"]
                self.assertIsInstance(detail, str)
                self.assertIn("exceeds chat_message_max_chars (", detail)
                self.assertIn(f"({cap} characters)", detail)
                self.assertNotIn("Q", detail)
                self.assertEqual(self._message_count(sid), 2)

    def test_max_chars_exact_cap_and_long_assistant_still_accepted(self):
        # Checked-in #922 contract: the cap bounds the LIVE user message;
        # exact-cap user content passes and legitimate assistant answers
        # (which can reach thinking_max_tokens) stay unbounded.
        cap = settings.chat_message_max_chars
        long_answer = "a" * (cap + 1)
        sid = self._seed_session()
        exact = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "user", "content": "q" * cap},
        )
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(self._message_count(sid), 3)
        single = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "assistant", "content": long_answer},
        )
        self.assertEqual(single.status_code, 200, single.text)
        batched = self.client.post(
            f"/api/chat/sessions/{sid}/messages/batch",
            json={"messages": [{"role": "assistant", "content": long_answer}]},
        )
        self.assertEqual(batched.status_code, 200, batched.text)

    def test_max_chars_enforced_only_after_auth_and_session_checks(self):
        # Enforcement-point ordering (the stream route's recorded reasoning):
        # 401 unauthenticated, 403 no vault access, 404 missing session —
        # never 422.
        cap = settings.chat_message_max_chars
        sid = self._seed_session()
        saved = app.dependency_overrides.pop(get_current_active_user)
        try:
            unauth = self.client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"role": "user", "content": "Q" * (cap + 1)},
            )
        finally:
            app.dependency_overrides[get_current_active_user] = saved
        self.assertEqual(unauth.status_code, 401, unauth.text)
        self._as(OUTSIDER)
        no_access = self.client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "user", "content": "Q" * (cap + 1)},
        )
        self.assertEqual(no_access.status_code, 403, no_access.text)
        self.assertIn("No write access", no_access.json()["detail"])
        self._as(MEMBER)
        missing = self.client.post(
            "/api/chat/sessions/999999/messages",
            json={"role": "user", "content": "Q" * (cap + 1)},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        for response in (unauth, no_access, missing):
            self.assertNotEqual(response.status_code, 422)


class QuotedFencePreservationTest(unittest.TestCase):
    """AC7: a fenced block nested in a block quote is preserved literally."""

    def test_blockquote_fence_variants_preserved_verbatim(self):
        cases = [
            "> ```xml\n<answer>keep me</answer>\n> ```",
            ">```\n<answer>keep me</answer>\n>```",
            "> ~~~\n<answer>keep me</answer>\n> ~~~",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                once = sanitize_assistant_content(raw)
                self.assertEqual(once, raw)
                self.assertEqual(sanitize_assistant_content(once), once)

    def test_blockquote_fence_wrapper_tags_stripped_outside_only(self):
        raw = "<answer>\n> ```xml\n<code>literal</code>\n> ```\n</answer>"
        expected = "> ```xml\n<code>literal</code>\n> ```"
        once = sanitize_assistant_content(raw)
        self.assertEqual(once, expected)
        self.assertEqual(sanitize_assistant_content(once), once)

    def test_blockquote_fence_indented_variant_content_survives(self):
        # Only survival of the literal tag is asserted: the function trims
        # the ends of the whole text, so leading whitespace is not a stable
        # expectation for this variant.
        raw = "  > ```\n<answer>keep me</answer>\n  > ```"
        once = sanitize_assistant_content(raw)
        self.assertIn("<answer>keep me</answer>", once)
        self.assertIn("<answer>keep me</answer>", sanitize_assistant_content(once))


class ResanitizeStabilityTest(unittest.TestCase):
    """AC8: re-sanitizing mixed wrapper/fence input is a no-op on pass 2."""

    def test_resanitize_idempotent_wrapper_sharing_fence_line(self):
        cases = [
            ("<answer>```\nSECRET\n```\n</answer>", "```\nSECRET\n```"),
            ("<answer>```lang\nSECRET\n```\n</answer>", "```lang\nSECRET\n```"),
            ("<answer>```\r\nSECRET\r\n```\r\n</answer>", "```\r\nSECRET\r\n```"),
            (
                "<answer>```\n<answer>SECRET</answer>\n```\n</answer>",
                "```\n<answer>SECRET</answer>\n```",
            ),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                once = sanitize_assistant_content(raw)
                self.assertEqual(once, expected)
                self.assertEqual(sanitize_assistant_content(once), once)

    def test_resanitize_idempotent_closer_line_variants_stay_stable(self):
        # Guard (passes today, must keep passing): when the closer line and
        # the wrapper tag share a line the output is already stable.
        for raw in ("```\nSECRET\n```</answer>", "```\nSECRET\n``` </answer>"):
            with self.subTest(raw=raw):
                once = sanitize_assistant_content(raw)
                self.assertEqual(once, raw)
                self.assertEqual(sanitize_assistant_content(once), once)


if __name__ == "__main__":
    unittest.main()
