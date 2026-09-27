"""Route-level pins for the issue #684 revision protocol.

Covers the truncate tail precondition (T1-13-S-05) and the durable fork anchor
(T1-13-K-02) beyond what the frozen checks pin: the equal-tail success, the
client-ahead refusal, the legitimate no-op at the boundary, the XOR anchor
validation, the nothing-to-fork refusal, the legacy out-of-bounds 400, and the
through_seq copy with aligned turn fields. Lives in its own file so the frozen
test_chat_turns.py bytes stay untouched.
"""

import pytest

from app.api.routes import chat as chat_routes
from app.models.database import init_db, run_migrations
from tests.test_chat_turns import _allow, _connect, _make_session, _mock_request, _msg


async def _seed_two_turns(conn, session_id: int) -> None:
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


def _count_rows(conn, session_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_truncate_expected_tail_equal_proceeds(tmp_path):
    """expected_tail_seq == current tail: the truncate applies normally."""
    db_path = tmp_path / "a02-tail-equal.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        result = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_seq=2, expected_tail_seq=4),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert result["remaining_count"] == 2
        assert result["tail_seq"] == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_truncate_expected_tail_greater_client_ahead_refused(tmp_path):
    """Client-ahead stale view (observed tail > current, e.g. after another
    writer truncated): refused with 409, zero rows deleted."""
    db_path = tmp_path / "a02-tail-ahead.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        with pytest.raises(chat_routes.HTTPException) as exc_info:
            await chat_routes.truncate_session_messages(
                _mock_request(),
                session_id,
                chat_routes.TruncateSessionRequest(keep_seq=0, expected_tail_seq=9),
                conn,
                {"id": 1},
                evaluate=_allow,
                _csrf_token="t",
            )
        assert exc_info.value.status_code == 409
        assert _count_rows(conn, session_id) == 4
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_truncate_noop_at_boundary_with_expected_tail_succeeds(tmp_path):
    """keep_seq == tail with expected == tail is a legitimate no-op SUCCESS
    (zero deletions), not a 409 — the race arm is conditioned on
    boundary < expected (#684 plan-critic round 2)."""
    db_path = tmp_path / "a02-noop-boundary.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        result = await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_seq=4, expected_tail_seq=4),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert result["remaining_count"] == 4
        assert result["tail_seq"] == 4
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_through_seq_copies_rows_and_aligns_turn_fields(tmp_path):
    """through_seq fork copies exactly the rows with seq <= anchor, renumbers
    them 1..n, and aligns the positional mode/turn side-fetches with the copy."""
    db_path = tmp_path / "a02-fork-anchor.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        forked = await chat_routes.fork_session(
            _mock_request(),
            session_id,
            chat_routes.ForkSessionRequest(through_seq=3),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert [m["content"] for m in forked["messages"]] == ["q-t1", "a-t1", "q-t2"]
        # Turn linkage is preserved positionally under the durable anchor.
        assert [m["turn_id"] for m in forked["messages"]] == ["t1", "t1", "t2"]
        assert [m["seq"] for m in forked["messages"]] == [1, 2, 3]
        assert forked["fork_message_index"] == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_through_seq_only_body_accepted(tmp_path):
    """A body carrying ONLY through_seq (no message_index) is valid — the
    fixed client sends exactly this shape (would 422 if message_index stayed
    required)."""
    db_path = tmp_path / "a02-fork-only-anchor.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        forked = await chat_routes.fork_session(
            _mock_request(),
            session_id,
            chat_routes.ForkSessionRequest(through_seq=2),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert [m["content"] for m in forked["messages"]] == ["q-t1", "a-t1"]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_requires_exactly_one_anchor(tmp_path):
    """Neither anchor, or both, is a 422 (XOR validation)."""
    db_path = tmp_path / "a02-fork-xor.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        for body in (
            chat_routes.ForkSessionRequest(),
            chat_routes.ForkSessionRequest(message_index=1, through_seq=2),
        ):
            with pytest.raises(chat_routes.HTTPException) as exc_info:
                await chat_routes.fork_session(
                    _mock_request(),
                    session_id,
                    body,
                    conn,
                    {"id": 1},
                    evaluate=_allow,
                    _csrf_token="t",
                )
            assert exc_info.value.status_code == 422
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_through_seq_matching_zero_rows_refused(tmp_path):
    """An anchor that selects zero rows is a clear 400 (no orphan empty fork
    session is created)."""
    db_path = tmp_path / "a02-fork-empty.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)
        # Clear every row, then anchor at a seq that matches nothing.
        await chat_routes.truncate_session_messages(
            _mock_request(),
            session_id,
            chat_routes.TruncateSessionRequest(keep_seq=0),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )

        with pytest.raises(chat_routes.HTTPException) as exc_info:
            await chat_routes.fork_session(
                _mock_request(),
                session_id,
                chat_routes.ForkSessionRequest(through_seq=1),
                conn,
                {"id": 1},
                evaluate=_allow,
                _csrf_token="t",
            )
        assert exc_info.value.status_code == 400
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_through_seq_beyond_tail_copies_whole_session(tmp_path):
    """An anchor beyond the current tail copies the WHOLE session (no data
    loss; the requesting client's view cannot be silently misrepresented) and
    records the effective cutoff (#684 plan: recorded decision + test)."""
    db_path = tmp_path / "a02-fork-beyond.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        forked = await chat_routes.fork_session(
            _mock_request(),
            session_id,
            chat_routes.ForkSessionRequest(through_seq=99),
            conn,
            {"id": 1},
            evaluate=_allow,
            _csrf_token="t",
        )
        assert [m["content"] for m in forked["messages"]] == [
            "q-t1",
            "a-t1",
            "q-t2",
            "a-t2",
        ]
        assert forked["fork_message_index"] == 3
    finally:
        conn.close()


def test_fork_through_seq_zero_rejected_by_validation():
    """through_seq=0 (zero durable rows must never be expressed on the wire)
    is rejected by the model's ge=1 constraint before any route logic."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        chat_routes.ForkSessionRequest(through_seq=0)


@pytest.mark.asyncio
async def test_fork_legacy_out_of_bounds_message_index_400(tmp_path):
    """Legacy positional fork past the end is a clear 400 (previously
    untested surface — 'C10 pins it' was wrong in the round-1 plan)."""
    db_path = tmp_path / "a02-fork-oob.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        with pytest.raises(chat_routes.HTTPException) as exc_info:
            await chat_routes.fork_session(
                _mock_request(),
                session_id,
                chat_routes.ForkSessionRequest(message_index=10),
                conn,
                {"id": 1},
                evaluate=_allow,
                _csrf_token="t",
            )
        assert exc_info.value.status_code == 400
    finally:
        conn.close()
