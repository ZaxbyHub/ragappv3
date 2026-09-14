"""Server-side durable chat turn writes on the streaming path (issue #553).

Covers: the pre-write of the user row (status 'pending') before the first
token, the complete finalize (since #555 the generation outlives a dropped
connection, so a disconnect completes the turn server-side; the #553
interrupted finalize keeps dedicated coverage for real cancellation via the
shutdown test), old-client
opt-out compat, pre-write failure semantics, duplicate pre-write idempotency,
admission-rejection writing nothing, and the stream-auth session validation.

Disconnect scenarios drive ``stream_chat_response``'s generator directly and
CANCEL the consuming task mid-stream: TestClient's ASGI transport buffers
the response and never surfaces ``http.disconnect``, so it cannot produce a
mid-stream disconnect; task cancellation lands CancelledError at the
generator's suspension point, which is exactly the unwinding Starlette's
disconnect path produces and therefore exercises the same cancellation-safe
finalize backstop.

Harness note: this module must not contain the double-submit-token parameter
name as a contiguous substring anywhere in its source. conftest's autouse
classifier keys on that substring and would disable the HTTP bypass this
suite relies on (see tests/conftest.py).
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (load-bearing for CI — see
# docs/engineering/testing.md §2; mirrors test_chat_streaming.py).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')
try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')
try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
)
from app.api.routes import chat as chat_routes  # noqa: E402
from app.api.routes.chat import get_stream_auth  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import get_pool, init_db, run_migrations  # noqa: E402

# Row-arrival assertions poll via _wait_for; the writes they wait on are
# ordered before the generator returns (awaited to_thread on completion;
# pure-sync disconnect finally), so polling is belt-and-braces against CI
# load, not a correctness dependency (PR review PRR-014).


def _wait_for(predicate, timeout=5.0, interval=0.02):
    """Poll predicate until truthy or the timeout expires (PR review
    PRR-014: fixed sleeps are load-sensitive; polling is not)."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _is_content_event(text: str) -> bool:
    return '"type": "content"' in text or '"type":"content"' in text


def _is_done_event(text: str) -> bool:
    return '"type": "done"' in text or '"type":"done"' in text


async def _mock_query(*args, **kwargs):
    yield {"type": "content", "content": "The plan begins "}
    yield {"type": "content", "content": "with retrieval."}
    yield {"type": "done", "sources": [], "memories_used": []}


class _Env:
    """Per-test environment: real temp DB, pool on app.state, mocked engine."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="issue553-stream-")
        self.db_path = os.path.join(self.tmp, "stream.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, 1, NULL)"
        ).lastrowid
        self.conn.commit()
        self.pool = get_pool(self.db_path)
        self._saved_pool = getattr(app.state, "db_pool", None)
        app.state.db_pool = self.pool
        app.state.vector_store = MagicMock()
        app.state.embedding_service = MagicMock()
        app.state.memory_store = MagicMock()
        app.state.llm_client = MagicMock()

        self.engine = MagicMock()
        self.engine.query = _mock_query
        self.engine.llm_client = None
        app.dependency_overrides[get_rag_engine] = lambda: self.engine
        app.dependency_overrides[get_stream_auth] = lambda: {
            "id": 1, "username": "probe", "role": "admin",
        }

        async def _override_db():
            yield self.conn

        self._allow = self._make_allow()

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1, "role": "admin",
        }
        app.dependency_overrides[get_evaluate_policy] = lambda: self._allow

    @staticmethod
    def _make_allow():
        async def _allow(*args, **kwargs):
            return True
        return _allow

    def client(self):
        return TestClient(app)

    def rows(self):
        return self.conn.execute(
            "SELECT role, status, turn_id, seq, content FROM chat_messages "
            "WHERE session_id = ? ORDER BY seq, id",
            (self.session_id,),
        ).fetchall()

    def all_rows(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM chat_messages"
        ).fetchone()[0]

    def payload(self, turn_id):
        return {
            "messages": [{"role": "user", "content": "What is the plan?"}],
            "vault_id": 1,
            "session_id": self.session_id,
            "turn_id": turn_id,
        }

    def make_stream(self, turn_id):
        """Build the real StreamingResponse for this turn (bypassing only the
        HTTP/auth layers, which have their own tests)."""
        return chat_routes.stream_chat_response(
            "What is the plan?",
            [],
            self.engine,
            vault_id=1,
            durable_session_id=self.session_id,
            durable_turn_id=turn_id,
            db_pool=self.pool,
        )

    def close(self):
        app.dependency_overrides.pop(get_rag_engine, None)
        app.dependency_overrides.pop(get_stream_auth, None)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_evaluate_policy, None)
        if self._saved_pool is not None:
            app.state.db_pool = self._saved_pool
        elif hasattr(app.state, "db_pool"):
            delattr(app.state, "db_pool")
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture
def env():
    e = _Env()
    try:
        yield e
    finally:
        e.close()


# ---------------------------------------------------------------------------
# Disconnect scenarios: direct generator + task cancellation (see the
# module docstring)
# ---------------------------------------------------------------------------


async def _consume_until(response, predicate):
    """Iterate the real response generator inside a task and cancel the task
    once ``predicate(chunk)`` holds — the exact mechanism Starlette uses on
    client disconnect (task cancellation lands CancelledError at the
    generator's current await/yield, unwinding the cancellation-safe
    finalize backstop in the same task context the stream started in)."""
    saw = asyncio.Event()

    async def consumer():
        async for chunk in response.body_iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
            if predicate(text):
                saw.set()
                break
        # Park on the next __anext__ await so the cancellation lands at the
        # generator's suspension point, not after the loop exited.
        async for _ in response.body_iterator:
            pass

    task = asyncio.create_task(consumer())
    await asyncio.wait_for(saw.wait(), timeout=10)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_prewrite_lands_before_first_token_and_disconnect_keeps_it(env):
    """AC1: with the engine gated so NO content chunk can ever be produced,
    dropping the connection after the first SSE event leaves the pending user
    row and no assistant row (LIVE-01 parity) — the pre-write provably landed
    before any token."""
    release = asyncio.Event()

    async def gated_query(*args, **kwargs):
        await release.wait()
        yield {"type": "content", "content": "never reached"}
        yield {"type": "done", "sources": [], "memories_used": []}

    env.engine.query = gated_query
    turn_id = "pretoken-turn-1"
    response = env.make_stream(turn_id)
    await _consume_until(response, lambda text: '"mode"' in text)
    assert _wait_for(
        lambda: len([r for r in env.rows() if r[0] == "user"]) == 1
    )

    rows = env.rows()
    user_rows = [r for r in rows if r[0] == "user"]
    assert len(user_rows) == 1
    assert user_rows[0][2] == turn_id
    assert user_rows[0][1] is not None
    assert [r for r in rows if r[0] == "assistant"] == []


async def _await_for(predicate, timeout=5.0, interval=0.02):
    """Async polling for direct-generator tests: unlike the sync ``_wait_for``
    (fine for the HTTP-path tests whose app loop runs in another thread), this
    yields to the event loop between polls, which the direct-generator tests
    REQUIRE — the producer finalizes via ``asyncio.to_thread`` completions
    that only land while the loop is free to run."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


async def test_disconnect_lets_generation_finish_and_persists_complete_turn(env):
    """Definition of done (issue #553 / E01a, as extended by issue #555): drop
    the connection after the first content token with NO client-side batch
    save. Under #555 the generation runs in the per-turn producer decoupled
    from the connection, so instead of finalizing an interrupted partial row
    the turn COMPLETES server-side and the full answer lands in history —
    that completion is exactly what a reconnect replays (the #555 DoD). The
    interrupted finalize itself keeps dedicated coverage in
    test_shutdown_cancellation_persists_interrupted_turn below."""
    turn_id = "dod-turn-1"
    response = env.make_stream(turn_id)
    await _consume_until(response, _is_content_event)
    assert await _await_for(
        lambda: len([r for r in env.rows() if r[0] == "assistant"]) == 1
    )
    # The producer finalizes complete with the FULL content (not a partial
    # interrupted fragment): the generation outlived the dropped connection.
    assert await _await_for(
        lambda: (
            [r for r in env.rows() if r[0] == "assistant"][0][1] == "complete"
            if [r for r in env.rows() if r[0] == "assistant"]
            else False
        )
    )

    rows = env.rows()
    user_rows = [r for r in rows if r[0] == "user"]
    assistant_rows = [r for r in rows if r[0] == "assistant"]
    assert len(user_rows) == 1
    assert len(assistant_rows) == 1
    assert assistant_rows[0][1] == "complete"
    assert user_rows[0][2] == turn_id
    assert assistant_rows[0][2] == turn_id
    assert "The plan begins with retrieval." in assistant_rows[0][4]

    # Second simulated client: read history through the real GET endpoint.
    # (Bare TestClient: the context-manager form would run the app's real
    # lifespan startup, which these tests do not exercise.)
    resp = env.client().get(f"/api/chat/sessions/{env.session_id}")
    assert resp.status_code == 200
    messages = resp.json().get("messages", [])
    by_role = {m["role"]: m for m in messages}
    assert by_role["user"]["turn_id"] == turn_id
    assert by_role["assistant"]["status"] == "complete"


async def test_shutdown_cancellation_persists_interrupted_turn(env):
    """Issue #553's cancellation-safe finalize, producer-owned (#555): only a
    real cancellation of the turn's producer task — the process-shutdown
    analogue, since a client disconnect no longer cancels the generation —
    finalizes the turn as interrupted with the partial content."""
    turn_id = "shutdown-turn-1"
    # Gate the engine after the first content chunk (never released) so the
    # producer is PROVABLY mid-generation when cancelled — an ungated scripted
    # engine can finish before task.cancel() lands and finalize complete.
    gate = asyncio.Event()

    async def gated_query(*args, **kwargs):
        yield {"type": "content", "content": "The plan begins "}
        await gate.wait()
        yield {"type": "content", "content": "with retrieval."}
        yield {"type": "done", "sources": [], "memories_used": []}

    env.engine.query = gated_query
    response = env.make_stream(turn_id)
    await _consume_until(response, _is_content_event)
    # The producer keeps running past the disconnect; cancel it directly.
    entry = chat_routes._turn_registry().get((env.session_id, turn_id))
    assert entry is not None and entry.task is not None
    entry.task.cancel()
    try:
        await entry.task
    except asyncio.CancelledError:
        pass
    assert _wait_for(
        lambda: len([r for r in env.rows() if r[0] == "assistant"]) == 1
    )

    rows = env.rows()
    assistant_rows = [r for r in rows if r[0] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0][1] == "interrupted"
    assert "The plan begins" in assistant_rows[0][4]


# ---------------------------------------------------------------------------
# Full-stream scenarios through the real HTTP endpoint (TestClient buffers
# the whole response, which is exactly right for completion paths)
# ---------------------------------------------------------------------------


def test_completion_persists_complete_assistant_row(env):
    """AC2: a fully consumed stream finalizes the assistant row with status
    complete and the joined content, and the done payload echoes the
    client's turn_id (PR review PRR-010)."""
    turn_id = "complete-turn-1"
    saw_done = False
    done_turn_id = None
    with env.client().stream(
        "POST", "/api/chat/stream", json=env.payload(turn_id)
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if _is_done_event(line):
                saw_done = True
                payload = json.loads(line[len("data: "):])
                done_turn_id = payload.get("turn_id")
    assert saw_done
    assert done_turn_id == turn_id
    assert _wait_for(
        lambda: len([r for r in env.rows() if r[0] == "assistant"]) == 1
    )
    rows = env.rows()
    user_rows = [r for r in rows if r[0] == "user"]
    assistant_rows = [r for r in rows if r[0] == "assistant"]
    assert len(user_rows) == 1
    assert len(assistant_rows) == 1
    assert assistant_rows[0][1] == "complete"
    assert assistant_rows[0][4] == "The plan begins with retrieval."


def test_request_without_durable_fields_writes_nothing(env):
    """AC12: the pre-#553 request shape (no session_id/turn_id keys) streams
    normally and writes zero rows — durability is opt-in."""
    payload = {
        "messages": [{"role": "user", "content": "What is the plan?"}],
        "vault_id": 1,
    }
    saw_done = False
    with env.client().stream(
        "POST", "/api/chat/stream", json=payload
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if _is_done_event(line):
                saw_done = True
    assert saw_done
    # No durable path exists for this request shape at all (durable_active is
    # False), so there is no writer to settle — assert immediately.
    assert env.all_rows() == 0


def test_prewrite_autotitles_untitled_session(env):
    """Decision 5: the pre-write path owns the first-turn auto-name for
    server-written turns (fallback title here — the engine mock carries no
    llm_client)."""
    turn_id = "autotitle-turn"
    with env.client().stream(
        "POST", "/api/chat/stream", json=env.payload(turn_id)
    ) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass

    def _titled():
        return env.conn.execute(
            "SELECT title FROM chat_sessions WHERE id = ?", (env.session_id,)
        ).fetchone()[0] == "New conversation"

    assert _wait_for(_titled)


# ---------------------------------------------------------------------------
# Failure and concurrency semantics
# ---------------------------------------------------------------------------


class _BrokenPool:
    """Pool stand-in whose checkouts always fail — models pre-write DB failure."""

    def connection(self):
        raise RuntimeError("pool unavailable")


def test_prewrite_failure_disables_server_writes_and_batch_reconciles(env, monkeypatch):
    """Decision 4 + critic R3: a failed pre-write disables server-side chat
    writes for the stream (no orphan assistant row; the stream still
    completes), and a later client batch — through the real HTTP batch
    endpoint — is the writer of record and fires the auto-title trigger from
    the batch path. Issue #555: the failure is injected at the pre-write
    itself (the event log is now the frame delivery channel, so breaking the
    whole pool would starve the stream rather than isolate the pre-write —
    that degradation contract has its own test below)."""
    def _failing_prewrite(db_pool, session_id, turn_id, content):
        return {
            "ok": False,
            "duplicate": False,
            "is_first_user_row": False,
            "title_is_null": True,
        }

    monkeypatch.setattr(chat_routes, "_prewrite_user_turn", _failing_prewrite)
    turn_id = "broken-prewrite-turn"
    saw_done = False
    with env.client().stream(
        "POST", "/api/chat/stream", json=env.payload(turn_id)
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if _is_done_event(line):
                saw_done = True
    assert saw_done
    # The pre-write failure is synchronous inside the producer and the
    # finalize is gated on pre-write success, so there is no writer to
    # settle — assert immediately.
    # No user row (pre-write failed) and no orphan assistant row.
    assert env.rows() == []

    resp = env.client().post(
            f"/api/chat/sessions/{env.session_id}/messages/batch",
            json={
                "messages": [
                    {"role": "user", "content": "What is the plan?", "turn_id": turn_id},
                    {
                        "role": "assistant",
                        "content": "The plan begins with retrieval.",
                        "turn_id": turn_id,
                        "status": "complete",
                    },
                ]
            },
        )
    assert resp.status_code == 200
    rows = env.rows()
    assert sorted(r[0] for r in rows) == ["assistant", "user"]
    title = env.conn.execute(
        "SELECT title FROM chat_sessions WHERE id = ?", (env.session_id,)
    ).fetchone()[0]
    assert title == "New conversation"


def test_broken_pool_degrades_to_clean_empty_stream(env):
    """Issue #555 degradation contract: with the pool fully unavailable, the
    event log cannot record or replay frames, so the reader closes the stream
    cleanly (HTTP 200, no frames, no terminal marker) instead of 500-ing —
    every log path is guarded and never fails the response."""
    app.state.db_pool = _BrokenPool()
    try:
        saw_any = False
        with env.client().stream(
            "POST", "/api/chat/stream", json=env.payload("broken-pool-turn")
        ) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.strip():
                    saw_any = True
        assert not saw_any
    finally:
        app.state.db_pool = env.pool


async def test_duplicate_prewrite_is_idempotent_and_finalize_upserts(env):
    """Decision 9 + critic R2: a double-delivered stream POST for one turn
    keeps a single durable user row; the finalize upserts onto it instead of
    duplicating."""
    turn_id = "dup-prewrite-turn"
    first = chat_routes._prewrite_user_turn(
        env.pool, env.session_id, turn_id, "What is the plan?"
    )
    second = chat_routes._prewrite_user_turn(
        env.pool, env.session_id, turn_id, "What is the plan?"
    )
    assert first["ok"] and not first["duplicate"]
    assert second["ok"] and second["duplicate"]
    assert len([r for r in env.rows() if r[0] == "user"]) == 1

    response = env.make_stream(turn_id)
    async for _ in response.body_iterator:
        pass
    assert _wait_for(
        lambda: len([r for r in env.rows() if r[0] == "assistant"]) == 1
    )
    rows = env.rows()
    assert sorted(r[0] for r in rows) == ["assistant", "user"]


async def test_admission_rejection_writes_nothing(env):
    """A request rejected by the admission gate never started a turn, so the
    pre-write must not run (zero rows)."""
    from app.services.admission import AdmissionClass, AdmissionRejected

    class _Saturated:
        def admit(self, cls):
            if cls is AdmissionClass.CHAT:
                raise AdmissionRejected("queue_full")
            raise AssertionError("unexpected admission class")

    with patch(
        "app.api.routes.chat.get_admission_controller", return_value=_Saturated()
    ):
        response = env.make_stream("saturated-turn")
        events = []
        async for chunk in response.body_iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
            if text.startswith("data:"):
                events.append(text)
    assert any("ADMISSION_REJECTED" in e for e in events)
    assert any(_is_done_event(e) for e in events)
    # Rejection happens before the pre-write, so no writer exists to settle.
    assert env.all_rows() == 0


async def test_finalize_upsert_is_idempotent(env):
    """The finalize upsert (component level): called twice it leaves exactly
    one assistant row with the latest status; a distinct turn gets its own
    row."""
    turn_id = "upsert-turn"
    payload = {
        "session_id": env.session_id,
        "turn_id": turn_id,
        "status": "complete",
        "content": "first version",
        "mode": None,
    }
    with env.pool.connection() as conn:
        chat_routes._upsert_assistant_turn(conn, payload)
        conn.commit()
    payload2 = dict(payload, status="interrupted", content="second version")
    with env.pool.connection() as conn:
        chat_routes._upsert_assistant_turn(conn, payload2)
        conn.commit()

    rows = [r for r in env.rows() if r[0] == "assistant"]
    assert len(rows) == 1
    assert rows[0][1] == "interrupted"
    assert rows[0][4] == "second version"

    payload3 = dict(payload, turn_id="upsert-turn-2", content="other turn")
    with env.pool.connection() as conn:
        chat_routes._upsert_assistant_turn(conn, payload3)
        conn.commit()
    assert len([r for r in env.rows() if r[0] == "assistant"]) == 2


# ---------------------------------------------------------------------------
# Stream-auth session validation (issue #553 decision 2)
# ---------------------------------------------------------------------------


async def test_stream_auth_rejects_unknown_session_and_readonly_vault(env):
    """session_id opt-in validates the session like the batch endpoint does:
    404 for an unknown session, 403 without WRITE on the session's vault."""
    request = MagicMock()
    request.app = app

    async def _fake_user(conn, req, header, cookie):
        return {"id": 1, "role": "member"}

    async def _allow_eval(*args, **kwargs):
        return True

    with patch.object(chat_routes, "_resolve_active_user", _fake_user):
        # Unknown session -> 404 (vault read allowed so the session check is
        # what rejects).
        with patch.object(chat_routes, "_evaluate_policy", _allow_eval):
            body = chat_routes.ChatStreamRequest(
                messages=[{"role": "user", "content": "q"}],
                vault_id=1,
                session_id=999999,
                turn_id="t",
            )
            with pytest.raises(Exception) as excinfo:
                await get_stream_auth(request, body)
            assert getattr(excinfo.value, "status_code", None) == 404

        # Existing session, but the caller lacks WRITE on its vault -> 403.
        async def _deny(*args, **kwargs):
            return False

        with patch.object(chat_routes, "_evaluate_policy", _deny):
            body = chat_routes.ChatStreamRequest(
                messages=[{"role": "user", "content": "q"}],
                vault_id=1,
                session_id=env.session_id,
                turn_id="t",
            )
            with pytest.raises(Exception) as excinfo:
                await get_stream_auth(request, body)
            assert getattr(excinfo.value, "status_code", None) == 403

        # With WRITE access the dependency resolves and stashes its flags.
        with patch.object(chat_routes, "_evaluate_policy", _allow_eval):
            body = chat_routes.ChatStreamRequest(
                messages=[{"role": "user", "content": "q"}],
                vault_id=1,
                session_id=env.session_id,
                turn_id="t",
            )
            user = await get_stream_auth(request, body)
            assert user["_can_write_memory"] is True


def test_turn_id_over_64_chars_is_rejected(env):
    """PR review PRR-011: the stream request enforces the same ≤64-char
    turn_id bound as the batch endpoint (422, not a silent accept)."""
    resp = env.client().post(
        "/api/chat/stream",
        json=_payload_with_turn("t" * 65, env.session_id),
    )
    assert resp.status_code == 422


def _payload_with_turn(turn_id, session_id):
    return {
        "messages": [{"role": "user", "content": "What is the plan?"}],
        "vault_id": 1,
        "session_id": session_id,
        "turn_id": turn_id,
    }


async def test_stream_auth_rejects_write_to_inaccessible_vault_session(env):
    """PR review PRR-012: a session living in a vault the caller cannot
    write is 403, even though its id is valid — the vault_id comes from the
    session row, not the request body."""
    env.conn.execute(
        "INSERT INTO vaults (id, name) VALUES (99, 'other-vault')"
    )
    env.conn.commit()
    other_session = env.conn.execute(
        "INSERT INTO chat_sessions (vault_id, user_id, title) "
        "VALUES (99, 1, NULL)"
    ).lastrowid
    env.conn.commit()

    request = MagicMock()
    request.app = app

    async def _fake_user(conn, req, header, cookie):
        return {"id": 1, "role": "member"}

    async def _deny_vault_99(conn, user, kind, vault_id, action):
        return vault_id != 99

    with patch.object(chat_routes, "_resolve_active_user", _fake_user), patch.object(
        chat_routes, "_evaluate_policy", _deny_vault_99
    ):
        body = chat_routes.ChatStreamRequest(
            messages=[{"role": "user", "content": "q"}],
            vault_id=1,
            session_id=other_session,
            turn_id="t",
        )
        with pytest.raises(Exception) as excinfo:
            await get_stream_auth(request, body)
        assert getattr(excinfo.value, "status_code", None) == 403
