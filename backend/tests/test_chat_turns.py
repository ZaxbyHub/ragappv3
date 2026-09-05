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
