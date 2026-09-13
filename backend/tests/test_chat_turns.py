"""Durable chat turn lifecycle tests (issue #507).

Covers the batch save endpoint (ordered, all-or-nothing), the truncate
revision endpoint, seq-based ordering across get_session/fork, turn-lifecycle
field round-trips (turn_id/status/assessments), legacy-row compatibility, the
seq-backfill migration, and the auto-naming title guard pin (CHAT-007,
already fixed at master — this suite pins the guard so a revert fails).
"""
import asyncio
import json
import os
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes import chat as chat_routes
from app.models.database import init_db, migrate_add_chat_turn_columns, run_migrations


def _mock_request():
    request = MagicMock(spec=Request)
    request.client.host = "127.0.0.1"
    return request


async def _allow(*args):
    return True


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _make_session(conn, title=None):
    return conn.execute(
        "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
        (1, 1, title),
    ).lastrowid


def _msg(role, content, **extra):
    payload = {"role": role, "content": content}
    payload.update(extra)
    return payload


class _FakePool:
    """Minimal get_pool stand-in exposing .connection() over a test conn."""

    def __init__(self, conn):
        self._conn = conn

    class _ConnCtx:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *exc):
            return False

    def connection(self):
        return _FakePool._ConnCtx(self._conn)


class _FakeLLM:
    def __init__(self, title):
        self._title = title
        self.calls = 0

    async def chat_completion(self, messages=None, temperature=None, max_tokens=None):
        self.calls += 1
        return self._title


# ---------------------------------------------------------------------------
# Batch save: ordering + field round-trip (CHAT-005, DEEP-D-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_saves_turn_in_order_with_all_fields(tmp_path):
    db_path = tmp_path / "turns.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        # Commit the seed row so the route's explicit BEGIN IMMEDIATE does not
        # collide with this connection's implicit seed transaction.
        conn.commit()
        turn_id = "11111111-1111-1111-1111-111111111111"
        assessments = {"[1]": 0.87}
        claims = ["Claim X could not be verified"]

        response = await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "Question"),
                    _msg(
                        "assistant",
                        "Answer",
                        turn_id=turn_id,
                        status="complete",
                        citation_confidence=assessments,
                        unverifiable_claims=claims,
                        wiki_refs=[{"wiki_label": "W1"}],
                        kms_refs=[{"kms_label": "K1"}],
                        mode="thinking",
                    ),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )

        saved = response["messages"]
        assert [m["role"] for m in saved] == ["user", "assistant"]
        # Durable ordering: the user row's seq is strictly below the assistant's.
        assert saved[0]["seq"] == 1
        assert saved[1]["seq"] == 2
        assert saved[1]["turn_id"] == turn_id
        assert saved[1]["status"] == "complete"
        assert saved[1]["citation_confidence"] == assessments
        assert saved[1]["unverifiable_claims"] == claims
        assert saved[1]["wiki_refs"] == [{"wiki_label": "W1"}]
        assert saved[1]["kms_refs"] == [{"kms_label": "K1"}]
        assert saved[1]["mode"] == "thinking"
        assert saved[0]["status"] is None

        # Round-trip through the session read path (ORDER BY seq).
        detail = await chat_routes.get_session(session_id, conn, {"id": 1}, evaluate=_allow)
        roles = [m["role"] for m in detail["messages"]]
        assert roles == ["user", "assistant"]
        assert detail["messages"][1]["citation_confidence"] == assessments
        assert detail["messages"][1]["unverifiable_claims"] == claims
        assert detail["messages"][1]["turn_id"] == turn_id
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_batch_rolls_back_completely_on_mid_batch_failure(tmp_path):
    """A failure after the first row must commit NOTHING (save failure after a
    partial write can never duplicate a successful sibling row)."""
    db_path = tmp_path / "turns-rollback.db"
    init_db(str(db_path))
    run_migrations(str(db_path))

    class _FailingConn(sqlite3.Connection):
        inserts = 0

        def execute(self, sql, parameters=(), /):
            if sql.lstrip().upper().startswith("INSERT INTO CHAT_MESSAGES"):
                _FailingConn.inserts += 1
                if _FailingConn.inserts > 1:
                    raise sqlite3.OperationalError("forced mid-batch failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(str(db_path), check_same_thread=False, factory=_FailingConn)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        session_id = _make_session(conn)
        conn.commit()
        _FailingConn.inserts = 0

        with pytest.raises(sqlite3.OperationalError):
            await chat_routes.add_messages_batch(
                _mock_request(),
                session_id,
                chat_routes.BatchAddMessagesRequest(
                    messages=[_msg("user", "Q"), _msg("assistant", "A")]
                ),
                conn,
                {"id": 1},
                evaluate=_allow,
                rag_engine=None,
                _csrf_token="t",
            )

        count = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        assert count == 0, "mid-batch failure must not leave any row committed"
    finally:
        conn.close()


# Windows sqlite3 can access-violate when two connections take and block on the
# writer lock from interleaved worker threads; CI (Linux) exercises the real
# concurrency path, Windows runs the rest of the suite.
@pytest.mark.skipif(sys.platform == "win32", reason="sqlite3 multithread crash on win32")
@pytest.mark.asyncio
async def test_concurrent_batches_assign_unique_monotonic_seq(tmp_path):
    """Two concurrent batch saves to one session must serialize on SQLite's
    write lock and produce UNIQUE seq values (gaps allowed, duplicates not)."""
    db_path = tmp_path / "turns-concurrent.db"
    init_db(str(db_path))
    run_migrations(str(db_path))

    conn_a = _connect(db_path)
    conn_b = _connect(db_path)
    try:
        session_id = _make_session(conn_a)
        conn_a.commit()

        async def run_batch(conn, content):
            return await chat_routes.add_messages_batch(
                _mock_request(),
                session_id,
                chat_routes.BatchAddMessagesRequest(
                    messages=[_msg("user", content), _msg("assistant", content + "!")]
                ),
                conn,
                {"id": 1},
                evaluate=_allow,
                rag_engine=None,
                _csrf_token="t",
            )

        results = await asyncio.gather(
            run_batch(conn_a, "turn-a"),
            run_batch(conn_b, "turn-b"),
        )
        seqs = []
        for response in results:
            for m in response["messages"]:
                seqs.append(m["seq"])
        assert len(set(seqs)) == len(seqs), f"seq values must be unique, got {seqs}"
        assert sorted(seqs) == [1, 2, 3, 4]

        detail = await chat_routes.get_session(session_id, conn_a, {"id": 1}, evaluate=_allow)
        roles = [m["role"] for m in detail["messages"]]
        assert roles.count("user") == 2 and roles.count("assistant") == 2
        # Every turn stays user-before-assistant regardless of arrival order.
        for i in range(0, 4, 2):
            assert detail["messages"][i]["role"] == "user"
            assert detail["messages"][i + 1]["role"] == "assistant"
    finally:
        conn_a.close()
        conn_b.close()


# ---------------------------------------------------------------------------
# Single-message path keeps assigning seq (old clients stay ordered)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_message_post_assigns_monotonic_seq(tmp_path):
    db_path = tmp_path / "turns-single.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        first = await chat_routes.add_message(
            _mock_request(),
            session_id,
            chat_routes.AddMessageRequest(role="user", content="Q"),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        second = await chat_routes.add_message(
            _mock_request(),
            session_id,
            chat_routes.AddMessageRequest(
                role="assistant", content="A", turn_id="t-1", status="complete"
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        assert first["seq"] == 1
        assert second["seq"] == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Truncate: retry/edit revision operation (CHAT-006)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncate_trims_tail_and_new_saves_continue_seq(tmp_path):
    db_path = tmp_path / "turns-truncate.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[_msg("user", "q1"), _msg("assistant", "a1")]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[_msg("user", "q2"), _msg("assistant", "a2")]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )

        result = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_count=2),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert result["remaining_count"] == 2
        assert result["tail_seq"] == 2

        # Retry resend continues the sequence without colliding.
        retry = await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[_msg("user", "q1-retry"), _msg("assistant", "a1-retry")]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        assert [m["seq"] for m in retry["messages"]] == [3, 4]

        detail = await chat_routes.get_session(session_id, conn, {"id": 1}, evaluate=_allow)
        assert [m["content"] for m in detail["messages"]] == ["q1", "a1", "q1-retry", "a1-retry"]

        # keep_count >= max seq is a no-op success.
        noop = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_count=99),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert noop["remaining_count"] == 4
    finally:
        conn.close()


def test_truncate_rejects_negative_keep_count():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        chat_routes.TruncateSessionRequest(keep_count=-1)


def test_batch_rejects_empty_messages_list():
    """PRR-014: min_length=1 on BatchAddMessagesRequest must reject an empty batch."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        chat_routes.BatchAddMessagesRequest(messages=[])


@pytest.mark.asyncio
async def test_truncate_requires_a_boundary(tmp_path):
    """PRR-020: with keep_seq now optional, the handler must 422 when the
    client supplies neither keep_seq nor keep_count."""
    db_path = tmp_path / "turns-boundary.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        with pytest.raises(chat_routes.HTTPException) as exc_info:
            await chat_routes.truncate_session_messages(
                _mock_request(),
                session_id,
                chat_routes.TruncateSessionRequest(),
                conn,
                {"id": 1},
                evaluate=_allow,
                _csrf_token="t",
            )
        assert exc_info.value.status_code == 422
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_truncate_keep_seq_anchors_at_durable_seq(tmp_path):
    """PRR-020: keep_seq anchors the DELETE at the highest durable seq the
    client keeps. This server-side test covers the boundary mechanics (0
    clears the session; a mid-history anchor trims exactly the tail above it
    and new saves continue from the anchor); the scenario where the anchor
    genuinely diverges from a positional count (locally-unpersisted rows) is
    falsified end-to-end by TranscriptPane.revision.test.tsx's divergence
    test, which asserts the stale positional value is NOT sent."""
    db_path = tmp_path / "turns-keepseq.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        for turn in ("t1", "t2"):
            await chat_routes.add_messages_batch(
                _mock_request(),
                session_id,
                chat_routes.BatchAddMessagesRequest(
                    messages=[
                        _msg("user", f"q-{turn}", turn_id=turn),
                        _msg("assistant", f"a-{turn}", turn_id=turn, status="complete"),
                    ]
                ),
                conn,
                {"id": 1},
                evaluate=_allow,
                rag_engine=None,
                _csrf_token="t",
            )

        # Local store simulates the divergence: rows 1-2 (turn t1) were never
        # persisted locally-visible... the client keeps only unpersisted rows,
        # so its durable anchor is 0 — every persisted row must go.
        cleared = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_seq=0),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert cleared["remaining_count"] == 0
        assert cleared["tail_seq"] in (0, None)

        # And anchoring mid-history trims only the tail above the anchor.
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "q-a", turn_id="ta"),
                    _msg("assistant", "a-a", turn_id="ta", status="complete"),
                    _msg("user", "q-b", turn_id="tb"),
                    _msg("assistant", "a-b", turn_id="tb", status="complete"),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        trimmed = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_seq=2),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert trimmed["remaining_count"] == 2
        assert trimmed["tail_seq"] == 2

        detail = await chat_routes.get_session(session_id, conn, {"id": 1}, evaluate=_allow)
        assert [m["content"] for m in detail["messages"]] == ["q-a", "a-a"]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_positional_turn_copy_survives_noncontiguous_seq(tmp_path):
    """PRR-015: fork copies turn fields by positionally matching two SELECTs
    both ordered (seq ASC, id ASC). Stress the positional invariant with
    non-contiguous seq values — the aligned case trivially agrees."""
    db_path = tmp_path / "turns-fork-gap.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "Question", turn_id="turn-9"),
                    _msg(
                        "assistant",
                        "Answer [K1]",
                        turn_id="turn-9",
                        status="interrupted",
                        citation_confidence={"[1]": 0.5},
                        unverifiable_claims=["claim"],
                    ),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        # Diverge seq from the trivial 1..n layout (post-truncate gap style).
        conn.execute("UPDATE chat_messages SET seq = 7 WHERE content = 'Question'")
        conn.execute("UPDATE chat_messages SET seq = 42 WHERE content = 'Answer [K1]'")
        conn.commit()

        response = await chat_routes.fork_session(
            _mock_request(),
            session_id,
            chat_routes.ForkSessionRequest(message_index=1),
            conn,
            {"id": 1},
            evaluate=_allow,
        )
        forked = response["messages"]
        # Fork renumbers to 1..n regardless of the source layout.
        assert [m["seq"] for m in forked] == [1, 2]
        # Positional matching still pairs each row with its own turn fields.
        assert forked[0]["turn_id"] == "turn-9"
        assert forked[1]["turn_id"] == "turn-9"
        assert forked[1]["status"] == "interrupted"
        assert forked[1]["citation_confidence"] == {"[1]": 0.5}
        assert forked[1]["unverifiable_claims"] == ["claim"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fork: turn fields preserved, seq renumbered (UI-039 / DEEP-D-01 / fork alignment)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_preserves_turn_fields_and_renumbers_seq(tmp_path):
    db_path = tmp_path / "turns-fork.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "Question", turn_id="turn-1"),
                    _msg(
                        "assistant",
                        "Answer [K1]",
                        turn_id="turn-1",
                        status="interrupted",
                        citation_confidence={"[1]": 0.5},
                        unverifiable_claims=["claim"],
                        kms_refs=[{"kms_label": "K1"}],
                        mode="instant",
                    ),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )

        response = await chat_routes.fork_session(
            _mock_request(),
            session_id,
            chat_routes.ForkSessionRequest(message_index=1),
            conn,
            {"id": 1},
            evaluate=_allow,
        )
        forked = response["messages"]
        assert [m["seq"] for m in forked] == [1, 2]
        assert forked[1]["turn_id"] == "turn-1"
        assert forked[1]["status"] == "interrupted"
        assert forked[1]["citation_confidence"] == {"[1]": 0.5}
        assert forked[1]["unverifiable_claims"] == ["claim"]
        assert forked[1]["kms_refs"] == [{"kms_label": "K1"}]
        assert forked[1]["mode"] == "instant"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Legacy rows: NULL turn fields, seq backfill migration
# ---------------------------------------------------------------------------


def test_migration_backfills_seq_for_legacy_rows_and_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        # Build the PRE-migration chat schema (no seq/turn/status/assessments).
        conn.executescript(
            """
            CREATE TABLE chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                user_id INTEGER,
                title TEXT,
                forked_from_session_id INTEGER,
                fork_message_index INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, 1, 'Legacy')"
        )
        for i, content in enumerate(["first", "second", "third"]):
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) "
                "VALUES (1, 'user', ?, ?)",
                (content, f"2026-01-0{i + 1} 00:00:00"),
            )
        conn.commit()

        migrate_add_chat_turn_columns(str(db_path))

        cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
        assert {"seq", "turn_id", "status", "citation_confidence", "unverifiable_claims"} <= cols

        rows = conn.execute(
            "SELECT content, seq FROM chat_messages ORDER BY seq ASC"
        ).fetchall()
        assert [content for content, _ in rows] == ["first", "second", "third"]
        assert [seq for _, seq in rows] == [1, 2, 3]

        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_chat_messages_session_seq'"
        ).fetchone()
        assert idx is not None

        # Idempotent re-run must not renumber.
        conn.execute("UPDATE chat_messages SET seq = 999 WHERE content = 'first'")
        conn.commit()
        migrate_add_chat_turn_columns(str(db_path))
        seq = conn.execute(
            "SELECT seq FROM chat_messages WHERE content = 'first'"
        ).fetchone()[0]
        assert seq == 999, "re-run must not renumber already-assigned seq values"
    finally:
        conn.close()


def test_migration_recovers_from_interrupted_backfill(tmp_path):
    """Reviewer MED fix: if the process died between the ALTER (auto-committed
    DDL) and the old added_seq-guarded backfill, the column existed with all
    NULL seqs and a re-run keyed on added_seq would never repopulate them.
    The NULL-probe guard must recover that state."""
    db_path = tmp_path / "legacy-interrupted.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        conn.executescript(
            """
            CREATE TABLE chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                user_id INTEGER,
                title TEXT,
                forked_from_session_id INTEGER,
                fork_message_index INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, 1, 'Interrupted')"
        )
        for i, content in enumerate(["a", "b"]):
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) "
                "VALUES (1, 'user', ?, ?)",
                (content, f"2026-02-0{i + 1} 00:00:00"),
            )
        conn.commit()
        # Simulate the interrupted migration: the column exists (auto-committed
        # ALTER) but the backfill never ran.
        conn.execute("ALTER TABLE chat_messages ADD COLUMN seq INTEGER")
        conn.commit()

        migrate_add_chat_turn_columns(str(db_path))

        rows = conn.execute(
            "SELECT content, seq FROM chat_messages ORDER BY seq ASC"
        ).fetchall()
        assert [content for content, _ in rows] == ["a", "b"]
        assert [seq for _, seq in rows] == [1, 2], (
            "a re-run must backfill rows left NULL by an interrupted migration"
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_legacy_rows_return_null_turn_fields(tmp_path):
    """Old rows (NULL status/turn fields) must read back unchanged — never
    invent evidence; the client mapper normalizes status NULL -> complete."""
    db_path = tmp_path / "legacy-read.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (?, 'user', 'old')",
            (session_id,),
        )
        conn.commit()
        detail = await chat_routes.get_session(session_id, conn, {"id": 1}, evaluate=_allow)
        msg = detail["messages"][0]
        assert msg["status"] is None
        assert msg["turn_id"] is None
        assert msg["citation_confidence"] is None
        assert msg["unverifiable_claims"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auto-naming title guard pin (CHAT-007 — already fixed; regression pin)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_name_guard_survives_manual_rename_and_competing_names(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "turns-title.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)

        # get_pool is patched, so the sqlite_path argument is irrelevant; do not
        # patch settings.sqlite_path (pydantic property has no setter).
        monkeypatch.setattr(chat_routes, "get_pool", lambda path: _FakePool(conn))

        # 1) NULL title -> generated title applied (untitled path).
        llm = _FakeLLM("Generated Title One")
        await chat_routes._auto_name_session(session_id, "What is a mango?", llm)
        assert conn.execute(
            "SELECT title FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()[0] == "Generated Title One"

        # 2) Competing auto-name request: the second generated title must NOT
        #    overwrite the first (generated titles never match the
        #    first-message prefix heuristic, so the guard rejects the update).
        llm2 = _FakeLLM("Generated Title Two")
        await chat_routes._auto_name_session(session_id, "What is a mango?", llm2)
        assert conn.execute(
            "SELECT title FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()[0] == "Generated Title One"

        # 3) Manual rename survives auto-naming (manual titles are not
        #    auto-looking and the atomic WHERE title=? guard rejects).
        conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ?",
            ("My Manual Research Title", session_id),
        )
        conn.commit()
        llm3 = _FakeLLM("Generated Title Three")
        await chat_routes._auto_name_session(session_id, "What is a mango?", llm3)
        assert conn.execute(
            "SELECT title FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()[0] == "My Manual Research Title"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Server-restart survival: turn data persists across pool/conn reopen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_survives_reopen_from_disk(tmp_path):
    db_path = tmp_path / "turns-restart.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    session_id = _make_session(conn)
    await chat_routes.add_messages_batch(
        _mock_request(),
        session_id,
        chat_routes.BatchAddMessagesRequest(
            messages=[
                _msg("user", "q", turn_id="turn-r"),
                _msg("assistant", "a", turn_id="turn-r", status="complete",
                     citation_confidence={"[1]": 0.9}),
            ]
        ),
        conn,
        {"id": 1},
        evaluate=_allow,
        rag_engine=None,
        _csrf_token="t",
    )
    conn.close()

    # "Restart": reopen the database file from disk with a fresh connection.
    conn2 = _connect(db_path)
    try:
        detail = await chat_routes.get_session(session_id, conn2, {"id": 1}, evaluate=_allow)
        msgs = detail["messages"]
        assert [m["content"] for m in msgs] == ["q", "a"]
        assert msgs[1]["status"] == "complete"
        assert msgs[1]["citation_confidence"] == {"[1]": 0.9}
        assert [m["seq"] for m in msgs] == [1, 2]
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Honesty-field durability (issue #510 AC-17/UI-004, review PRR-002/PRR-003)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_message_echoes_persisted_honesty_fields(tmp_path):
    """PRR-003: the single-message endpoint must echo the honesty fields it
    persists, matching the batch endpoint's response surface."""
    db_path = tmp_path / "echo.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        warnings = ["Superseded by a newer version."]
        enforcement = {"mode": "required", "status": "missing_citations"}
        response = await chat_routes.add_message(
            _mock_request(),
            session_id,
            chat_routes.AddMessageRequest(
                role="assistant",
                content="Answer.",
                currency_warnings=warnings,
                citation_enforcement=enforcement,
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        assert response["currency_warnings"] == warnings
        assert response["citation_enforcement"] == enforcement
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_preserves_honesty_fields_across_reload(tmp_path):
    """PRR-002: fork_session must carry currency_warnings and
    citation_enforcement into the forked rows so a forked-session reload
    keeps the original turn's honesty evidence."""
    db_path = tmp_path / "fork-honesty.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        warnings = ["Superseded by a newer version."]
        enforcement = {"mode": "required", "status": "satisfied"}
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "Question", turn_id="turn-1"),
                    _msg(
                        "assistant",
                        "Answer [S1].",
                        turn_id="turn-1",
                        status="complete",
                        currency_warnings=warnings,
                        citation_enforcement=enforcement,
                    ),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )

        response = await chat_routes.fork_session(
            _mock_request(),
            session_id,
            chat_routes.ForkSessionRequest(message_index=1),
            conn,
            {"id": 1},
            evaluate=_allow,
        )
        forked = response["messages"]
        assert forked[1]["currency_warnings"] == warnings
        assert forked[1]["citation_enforcement"] == enforcement
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Issue #553: server-side turn writes, turn_id reconcile, uniqueness
# ---------------------------------------------------------------------------


def _prewrite_pending_row(conn, session_id, turn_id, content="Question"):
    """Insert the server-side pending user row the way the stream pre-write
    does (issue #553): plain INSERT + seq/turn_id/status side-write."""
    cursor = conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, created_at) "
        "VALUES (?, 'user', ?, CURRENT_TIMESTAMP)",
        (session_id, content),
    )
    pre_id = cursor.lastrowid
    conn.execute(
        "UPDATE chat_messages SET seq = (SELECT COALESCE(MAX(seq), 0) + 1 "
        "FROM chat_messages WHERE session_id = ?), "
        "turn_id = ?, status = 'pending' WHERE id = ?",
        (session_id, turn_id, pre_id),
    )
    conn.commit()
    pre_row = conn.execute(
        "SELECT id, seq FROM chat_messages WHERE id = ?", (pre_id,)
    ).fetchone()
    return pre_row


@pytest.mark.asyncio
async def test_batch_reconciles_server_prewritten_turn_in_place(tmp_path):
    """A batch save for a turn the server already pre-wrote (status pending)
    UPDATEs the durable row (id and seq preserved) instead of duplicating it,
    and the response returns the reconciled durable ids (issue #553)."""
    db_path = tmp_path / "turns-reconcile.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        turn_id = "recon-turn-1"
        pre_row = _prewrite_pending_row(conn, session_id, turn_id)

        response = await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "Question", turn_id=turn_id),
                    _msg("assistant", "Answer", turn_id=turn_id, status="complete"),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )

        rows = conn.execute(
            "SELECT id, role, status, turn_id, seq FROM chat_messages "
            "WHERE session_id = ? ORDER BY seq, id",
            (session_id,),
        ).fetchall()
        user_rows = [r for r in rows if r[1] == "user"]
        assistant_rows = [r for r in rows if r[1] == "assistant"]
        assert len(user_rows) == 1, f"expected one user row, got {rows}"
        assert len(assistant_rows) == 1
        # The reconcile updated the pre-written row in place: same id, same seq.
        assert user_rows[0][0] == pre_row[0]
        assert user_rows[0][4] == pre_row[1]
        # The assistant row is a fresh insert with the next seq.
        assert assistant_rows[0][4] == pre_row[1] + 1
        # The response carries the reconciled durable ids (never lastrowid
        # placeholders) so the client adopts the existing rows.
        assert [m["id"] for m in response["messages"]] == [
            pre_row[0],
            assistant_rows[0][0],
        ]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_duplicate_batch_cannot_duplicate_turn(tmp_path):
    """Replaying the identical batch payload for one turn_id twice leaves
    exactly one row pair — the reconcile is idempotent (issue #553; named
    alongside test_concurrent_batches_assign_unique_monotonic_seq by the
    issue's required-tests section)."""
    db_path = tmp_path / "turns-dup-batch.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        turn_id = "dup-turn-1"
        body = chat_routes.BatchAddMessagesRequest(
            messages=[
                _msg("user", "Question", turn_id=turn_id),
                _msg("assistant", "Answer", turn_id=turn_id, status="complete"),
            ]
        )
        for _ in range(2):
            await chat_routes.add_messages_batch(
                _mock_request(),
                session_id,
                body,
                conn,
                {"id": 1},
                evaluate=_allow,
                rag_engine=None,
                _csrf_token="t",
            )

        rows = conn.execute(
            "SELECT role, turn_id FROM chat_messages WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        assert sorted(r[0] for r in rows) == ["assistant", "user"]
    finally:
        conn.close()


def test_unique_index_rejects_duplicate_turn_role_rows(tmp_path):
    """The (session_id, turn_id, role) partial unique index rejects a second
    row for the same role in one turn, allows the user+assistant pair, and
    never conflicts on legacy NULL turn_id rows (issue #553)."""
    db_path = tmp_path / "turns-unique.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()

        def insert(role, content, turn_id):
            return conn.execute(
                "INSERT INTO chat_messages "
                "(session_id, role, content, seq, turn_id, created_at) "
                "VALUES (?, ?, ?, "
                "(SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_messages "
                " WHERE session_id = ?), ?, CURRENT_TIMESTAMP)",
                (session_id, role, content, session_id, turn_id),
            )

        insert("user", "first", "turn-u1")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            insert("user", "second", "turn-u1")
            conn.commit()
        conn.rollback()
        # The user+assistant PAIR for one turn is allowed (roles differ).
        insert("assistant", "reply", "turn-u1")
        conn.commit()
        # Legacy NULL turn_id rows never conflict.
        insert("assistant", "legacy-1", None)
        insert("assistant", "legacy-2", None)
        conn.commit()
    finally:
        conn.close()


def test_migration_dedupe_keeps_most_informative_row(tmp_path):
    """The one-time pre-index dedupe keeps exactly one row per
    (session, turn, role) preferring 'complete' over 'interrupted'/'failed'
    over 'pending'/NULL, then longest content, then highest id; a re-run is a
    no-op once the index exists (issue #553)."""
    db_path = tmp_path / "turns-dedupe.db"
    # Seed a post-#507, pre-#553 database: the turn columns exist (and hold
    # duplicate rows from client double-saves) but the unique index does not.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS chat_messages;
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                sources TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                seq INTEGER,
                turn_id TEXT,
                status TEXT
            );
            CREATE TABLE chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                user_id INTEGER,
                title TEXT,
                forked_from_session_id INTEGER,
                fork_message_index INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        session_id = conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, 1, NULL)"
        ).lastrowid
        # One turn, role='user', three duplicate writes from pre-#553
        # double-saves with different statuses; plus a NULL-turn legacy row
        # and a clean second turn that must be untouched.
        rows = [
            (session_id, "user", "pending variant", None, 1),          # pending
            (session_id, "user", "interrupted variant longer", None, 1),  # interrupted, longest
            (session_id, "user", "complete answer", None, 1),          # complete, short
            (session_id, "assistant", "legacy row", None, None),       # NULL turn
            (session_id, "user", "other turn", "turn-2", 2),           # other turn
        ]
        for sid, role, content, turn, _ in rows:
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (sid, role, content),
            )
        # Stamp turn_id/status on the duplicate set the way #507 clients did.
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM chat_messages ORDER BY id"
        ).fetchall()]
        conn.execute("UPDATE chat_messages SET turn_id='turn-1', status='pending' WHERE id=?", (ids[0],))
        conn.execute("UPDATE chat_messages SET turn_id='turn-1', status='interrupted' WHERE id=?", (ids[1],))
        conn.execute("UPDATE chat_messages SET turn_id='turn-1', status='complete' WHERE id=?", (ids[2],))
        conn.execute("UPDATE chat_messages SET turn_id='turn-2', status='complete' WHERE id=?", (ids[4],))
        conn.commit()
    finally:
        conn.close()

    migrate_add_chat_turn_columns(str(db_path))

    conn = _connect(db_path)
    try:
        survivors = conn.execute(
            "SELECT turn_id, role, status, content FROM chat_messages "
            "WHERE turn_id = 'turn-1' ORDER BY id"
        ).fetchall()
        # Exactly one survivor for turn-1/user: status priority puts
        # 'complete' first even though 'interrupted' is longer.
        assert len(survivors) == 1
        assert survivors[0][2] == "complete"
        # NULL-turn legacy row and the clean second turn untouched.
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE turn_id IS NULL"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE turn_id = 'turn-2'"
        ).fetchone()[0] == 1
        # The unique index now exists.
        index_names = {
            row[1] for row in conn.execute("PRAGMA index_list(chat_messages)")
        }
        assert "idx_chat_messages_session_turn_role" in index_names
    finally:
        conn.close()

    # Re-running the migration is a no-op (index exists → no re-delete).
    migrate_add_chat_turn_columns(str(db_path))
    conn = _connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE turn_id = 'turn-1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_keeps_turn_fields_after_prewrite_and_reconcile(tmp_path):
    """Fork copies turn fields by position over (seq, id) order; a
    server-pre-written user row reconciled by a later batch must keep its seq
    so the fork still pairs the turn's rows correctly (issue #553)."""
    db_path = tmp_path / "turns-fork-recon.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        turn_id = "fork-turn-1"
        _prewrite_pending_row(conn, session_id, turn_id)
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "Question", turn_id=turn_id),
                    _msg("assistant", "Answer", turn_id=turn_id, status="complete"),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        response = await chat_routes.fork_session(
            _mock_request(),
            session_id,
            chat_routes.ForkSessionRequest(message_index=1),
            conn,
            {"id": 1},
            evaluate=_allow,
        )
        forked = response["messages"]
        assert [m["role"] for m in forked] == ["user", "assistant"]
        assert {m["turn_id"] for m in forked} == {turn_id}
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_truncate_keep_seq_with_server_prewritten_pending_tail(tmp_path):
    """Issue #553 AC9: with a server-pre-written pending row at the tail
    (the stream pre-write's row, mid-generation), truncate keep_seq still
    computes the correct boundary - the pending tail is removed, the counts
    are exact, and post-truncate batch saves continue the seq monotonically
    from the anchor. Shipped twin of frozen checks C8/C9."""
    db_path = tmp_path / "turns-truncate-pending.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        # One completed turn (user seq 1 + assistant seq 2, NULL turn ids -
        # legacy shape is still legal under the partial index), then the
        # server pre-write lands the NEXT turn's user row (seq 3, pending)
        # and generation is cut off before any assistant row exists.
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "first question"),
                    _msg("assistant", "first answer", status="complete"),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        _prewrite_pending_row(conn, session_id, "pending-turn-1", "second question")

        rows_before = conn.execute(
            "SELECT seq, status FROM chat_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        assert [r[0] for r in rows_before] == [1, 2, 3]
        assert rows_before[2][1] == "pending"

        # Truncate anchored at the completed turn (keep_seq=2): the pending
        # tail must go with everything above the boundary.
        result = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_seq=2),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert result["remaining_count"] == 2
        assert result["tail_seq"] == 2
        remaining = conn.execute(
            "SELECT seq, status FROM chat_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        assert [r[0] for r in remaining] == [1, 2]
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE session_id = ? AND status = 'pending'",
            (session_id,),
        ).fetchone()[0] == 0

        # keep_seq=0 clears everything including a newly pre-written row.
        _prewrite_pending_row(conn, session_id, "pending-turn-2", "third question")
        result = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_seq=0),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert result["remaining_count"] == 0
        assert result["tail_seq"] == 0

        # After the clear, a new batch save continues seq monotonically from
        # the current max (the invariant is unique monotonic seq per session).
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", "retry question"),
                    _msg("assistant", "retry answer", status="complete"),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
        seqs = [r[0] for r in conn.execute(
            "SELECT seq FROM chat_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()]
        assert len(seqs) == len(set(seqs))
        assert seqs == sorted(seqs)
        assert len(seqs) == 2
    finally:
        conn.close()
