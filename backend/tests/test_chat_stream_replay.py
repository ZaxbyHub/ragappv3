"""Resumable SSE chat streams: per-turn event log + Last-Event-ID (issue #555).

Route- and unit-level contract tests complementing the frozen acceptance
checks (repro/C1-C8): header parsing, producer/reader decoupling, registry
dedupe, log hygiene, and the honest close when the producer is gone. Harness
pattern mirrors test_chat_stream_durability.py (real temp DB, real pool,
scripted fake engine, direct generator consumption with task-cancellation
disconnects; TestClient only where the route layer itself is under test).

Module note: this file must not contain the double-submit-token parameter
name as a contiguous substring (conftest's autouse classifier keys on it).
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


def _wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


async def _await_for(predicate, timeout=5.0, interval=0.02):
    """Async polling: yields to the loop so producer to_thread completions
    can land (direct-generator tests must not block the loop)."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


def _scripted_query(*args, **kwargs):
    async def _gen():
        yield {"type": "content", "content": "alpha "}
        yield {"type": "content", "content": "beta "}
        yield {"type": "done", "sources": [], "memories_used": []}
    return _gen()


def _make_gen(*chunks_and_done):
    """Build an async-gen FUNCTION yielding the given content chunks + done."""

    def _query(*args, **kwargs):
        async def _gen():
            for chunk in chunks_and_done:
                yield {"type": "content", "content": chunk}
            yield {"type": "done", "sources": [], "memories_used": []}
        return _gen()

    return _query


def _frames(wire: str):
    """Parse SSE wire text into (id, data_json) event blocks, comments kept
    as (None, ':...')."""
    out = []
    for block in wire.split("\n\n"):
        if not block.strip("\r\n \t"):
            continue
        eid = None
        data = None
        for line in block.split("\n"):
            line = line.rstrip("\r")
            if line.startswith(":"):
                out.append((None, line))
                break
            if line.startswith("id:"):
                eid = line[3:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if data is not None:
            out.append((eid, data))
    return out


class _Env:
    """Real temp DB + pool on app.state + scripted engine, two sessions."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="issue555-replay-")
        self.db_path = os.path.join(self.tmp, "replay.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, 1, NULL)"
        ).lastrowid
        self.other_session_id = self.conn.execute(
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
        self.engine.query = MagicMock(side_effect=_scripted_query)
        self.engine.llm_client = None
        app.dependency_overrides[get_rag_engine] = lambda: self.engine
        app.dependency_overrides[get_stream_auth] = lambda: {
            "id": 1, "username": "replay", "role": "admin",
        }

        async def _override_db():
            yield self.conn

        async def _allow(*args, **kwargs):
            return True

        self._allow = _allow
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1, "role": "admin",
        }
        app.dependency_overrides[get_evaluate_policy] = _allow

    def make_stream(self, turn_id, session_id=None, last_event_id=None):
        return chat_routes.stream_chat_response(
            "What is the plan?",
            [],
            self.engine,
            vault_id=1,
            durable_session_id=self.session_id if session_id is None else session_id,
            durable_turn_id=turn_id,
            db_pool=self.pool,
            last_event_id=last_event_id,
        )

    def rows(self):
        return self.conn.execute(
            "SELECT role, status, turn_id, seq, content FROM chat_messages "
            "WHERE session_id = ? ORDER BY seq, id",
            (self.session_id,),
        ).fetchall()

    def event_rows(self, turn_id, session_id=None):
        return self.conn.execute(
            "SELECT seq, payload FROM chat_stream_events "
            "WHERE session_id = ? AND turn_id = ? ORDER BY seq",
            (self.session_id if session_id is None else session_id, turn_id),
        ).fetchall()

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


async def _collect_all(response):
    frames = []
    async for chunk in response.body_iterator:
        frames.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
    return frames


async def _consume_until(response, predicate):
    saw = asyncio.Event()

    async def consumer():
        async for chunk in response.body_iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
            if predicate(text):
                saw.set()
                break
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


# ---------------------------------------------------------------------------
# Route layer: Last-Event-ID header parsing
# ---------------------------------------------------------------------------


def test_malformed_last_event_id_is_a_400(env):
    """A non-integer Last-Event-ID must be a 400: silently ignoring a resume
    position would make a reconnecting client see duplicated frames."""
    payload = {
        "messages": [{"role": "user", "content": "What is the plan?"}],
        "vault_id": 1,
        "session_id": env.session_id,
        "turn_id": "malformed-id-turn",
    }
    client = TestClient(app)
    response = client.post(
        "/api/chat/stream",
        json=payload,
        headers={"Last-Event-ID": "not-a-number"},
    )
    assert response.status_code == 400
    assert "Last-Event-ID" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Producer/reader decoupling: registry dedupe + regenerate seq reset
# ---------------------------------------------------------------------------


async def test_double_first_post_starts_one_generation(env):
    """A duplicate POST for a live turn (double click, proxy retry) attaches
    as a second reader instead of starting a second generation — the engine
    runs exactly once."""
    turn_id = "dedupe-turn"
    first = env.make_stream(turn_id)
    second = env.make_stream(turn_id)
    calls_before = env.engine.query.call_count
    frames_a, frames_b = await asyncio.gather(
        _collect_all(first), _collect_all(second)
    )
    assert env.engine.query.call_count - calls_before == 1
    # Both connections receive the same full frame stream (per-connection
    # exactly-once).
    assert frames_a == frames_b
    joined = "".join(frames_a)
    assert joined.count('"alpha "') == 1 and joined.count('"beta "') == 1


async def test_regenerate_purges_stale_frames_and_logs_new_generation(env):
    """A fresh generation for a finished turn (client retry semantics) purges
    the turn's old event rows and logs the NEW generation from seq 1. The
    second generation uses DISTINCT content so the assertion proves the new
    payloads actually landed (identical payloads could pass on stale rows)."""
    turn_id = "regen-turn"
    first_gen = _make_gen("alpha ", "beta ")
    second_gen = _make_gen("gamma ", "delta ")
    env.engine.query = MagicMock(side_effect=[first_gen(), second_gen()])

    await _collect_all(env.make_stream(turn_id))
    assert await _await_for(lambda: len(env.event_rows(turn_id)) == 4)
    stale_payloads = [r[1] for r in env.event_rows(turn_id)]
    assert any('"alpha "' in p for p in stale_payloads)
    # The first producer must be fully retired (registry popped by its
    # done-callback) before the regenerate POST, or the second POST would
    # attach as a reader of the finished producer instead of regenerating.
    assert await _await_for(
        lambda: chat_routes._turn_registry().get((env.session_id, turn_id)) is None
    )

    reconnect_wire = "".join(await _collect_all(env.make_stream(turn_id)))
    assert await _await_for(lambda: len(env.event_rows(turn_id)) == 4)
    rows = env.event_rows(turn_id)
    seqs = [r[0] for r in rows]
    payloads = [r[1] for r in rows]
    # The log restarts at seq 1 and holds the NEW generation's frames only.
    assert seqs == [1, 2, 3, 4]
    assert any('"gamma "' in p for p in payloads)
    assert any('"delta "' in p for p in payloads)
    assert not any('"alpha "' in p for p in payloads)
    assert not any('"beta "' in p for p in payloads)
    # The regenerate connection itself streamed the new answer (stale frames
    # were never served on the wire).
    assert '"gamma "' in reconnect_wire
    assert '"alpha "' not in reconnect_wire


async def test_cross_session_turn_id_isolation(env):
    """turn_ids are unique per session: a reconnect naming session B cannot
    read session A's logged frames."""
    turn_id = "shared-turn-id"
    await _collect_all(env.make_stream(turn_id))
    assert await _await_for(lambda: len(env.event_rows(turn_id)) == 4)
    reconnect = env.make_stream(
        turn_id, session_id=env.other_session_id, last_event_id=0
    )
    frames = await _collect_all(reconnect)
    assert frames == []


async def test_retention_purges_older_turn_rows_on_new_turn(env):
    """When a new turn starts in a session, the session's older turns' event
    rows are purged (only the newest turn stays resumable)."""
    old_turn = "old-turn"
    await _collect_all(env.make_stream(old_turn))
    assert await _await_for(lambda: len(env.event_rows(old_turn)) == 4)
    await _collect_all(env.make_stream("new-turn"))
    assert await _await_for(lambda: env.event_rows(old_turn) == [])


# ---------------------------------------------------------------------------
# Honest close: a producer gone without a terminal frame never fabricates one
# ---------------------------------------------------------------------------


async def test_reconnect_after_producer_death_replays_without_done(env):
    """Kill the producer mid-generation (the connection-drop-only scope's
    honest boundary; full process-kill resume is I4/#559-gated): a reconnect
    replays the persisted frames after its last id and closes WITHOUT a done
    frame — the client sees an interrupted, retryable turn, never a fabricated
    completion."""
    turn_id = "dead-process-turn"
    # Simulate what a killed process leaves behind: persisted frames with NO
    # terminal frame (a producer cancellation in-process would append the
    # error+done pair; only process death skips it) and no live producer.
    seeded = [
        (env.session_id, turn_id, 1, json.dumps({"type": "mode", "mode": "thinking"})),
        (env.session_id, turn_id, 2, json.dumps({"type": "content", "content": "alpha "})),
        (env.session_id, turn_id, 3, json.dumps({"type": "content", "content": "beta "})),
    ]
    env.conn.executemany(
        "INSERT INTO chat_stream_events (session_id, turn_id, seq, payload) VALUES (?, ?, ?, ?)",
        seeded,
    )
    env.conn.commit()
    assert chat_routes._turn_registry().get((env.session_id, turn_id)) is None

    reconnect = env.make_stream(turn_id, last_event_id=1)
    frames = await _collect_all(reconnect)
    parsed = _frames("".join(frames))
    ids = [int(eid) for eid, _ in parsed if eid is not None]
    assert ids == [2, 3]  # replays strictly after the client's last id
    types = [json.loads(data).get("type") for _, data in parsed if data]
    assert "done" not in types  # no fabricated completion
    # A second reconnect with the FULL log present but still no terminal
    # behaves the same: replay everything, close honestly, never 5xx.
    reconnect_all = env.make_stream(turn_id, last_event_id=0)
    frames_all = await _collect_all(reconnect_all)
    types_all = [json.loads(data).get("type") for _, data in _frames("".join(frames_all)) if data]
    assert types_all == ["mode", "content", "content"]


async def test_reconnect_after_completion_replays_missed_through_done(env):
    """Disconnect, let the generation finish server-side, reconnect with the
    last received id: exactly the missed frames arrive, ending with done, and
    the engine is NOT re-run (the issue's definition of done)."""
    turn_id = "reconnect-done-turn"
    first = env.make_stream(turn_id)
    await _consume_until(first, lambda text: '"alpha "' in text)
    assert await _await_for(lambda: len(env.event_rows(turn_id)) == 4)
    calls = env.engine.query.call_count
    reconnect = env.make_stream(turn_id, last_event_id=1)
    frames = await _collect_all(reconnect)
    parsed = _frames("".join(frames))
    ids = [int(eid) for eid, _ in parsed if eid is not None]
    assert ids == [2, 3, 4]
    types = [json.loads(data).get("type") for _, data in parsed if data]
    assert types == ["content", "content", "done"]
    assert env.engine.query.call_count == calls


async def test_non_durable_stream_stays_connection_bound(env):
    """Without session/turn/pool the stream is exactly pre-#553: completes
    normally with plain data frames, writes nothing to chat tables, and logs
    no event rows."""
    response = chat_routes.stream_chat_response(
        "What is the plan?",
        [],
        env.engine,
        vault_id=1,
        durable_session_id=None,
        durable_turn_id=None,
        db_pool=None,
    )
    frames = await _collect_all(response)
    joined = "".join(frames)
    assert joined.count('"alpha "') == 1 and joined.count('"beta "') == 1
    assert '"type": "done"' in joined
    assert all(eid is None for eid, _ in _frames(joined))
    assert env.rows() == []
    assert env.event_rows("anything") == []
