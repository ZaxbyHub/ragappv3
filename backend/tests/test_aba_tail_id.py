"""Issue #684 review (ABA): the expected_tail_seq precondition alone is
defeated by per-session seq reuse — another writer's truncate+resave restores
MAX(seq) to a coincidentally equal value. The ABA-proof precondition is the
tail row's PRIMARY KEY (AUTOINCREMENT, never reused). Pins both arms."""

import pytest

from app.api.routes import chat as chat_routes
from app.models.database import init_db, run_migrations
from tests.test_chat_turns import _allow, _connect, _make_session, _mock_request, _msg


async def _seed_two_turns(conn, session_id: int, prefix: str = "") -> None:
    turns = ("t1", "t2") if not prefix else ("b1", "b2")
    for turn in turns:
        await chat_routes.add_messages_batch(
            _mock_request(),
            session_id,
            chat_routes.BatchAddMessagesRequest(
                messages=[
                    _msg("user", f"{prefix}q-{turn}", turn_id=turn),
                    _msg("assistant", f"{prefix}a-{turn}", turn_id=turn, status="complete"),
                ]
            ),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )


def _rows(conn, session_id: int):
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT id, seq FROM chat_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    ]


@pytest.mark.asyncio
async def test_aba_tail_id_refuses_stale_truncate_after_seq_reuse(tmp_path):
    """Tab A observes tail (seq=4, id=4). Tab B truncates all and resaves two
    turns: seqs are REUSED 1..4 but the row ids are NEW (5..8). Tab A's stale
    truncate — whose seq-only precondition would pass on the coincidental
    equal tail — must be refused when it carries the (now stale)
    expected_tail_id, and Tab B's rows must be intact."""
    db_path = tmp_path / "aba-refuse.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)
        assert _rows(conn, session_id) == [(1, 1), (2, 2), (3, 3), (4, 4)]

        # Tab B: truncate all, resave two turns -> seqs reused, ids advanced.
        await chat_routes.truncate_session_messages(
            _mock_request(), session_id,
            chat_routes.TruncateSessionRequest(keep_seq=0),
            conn, {"id": 1}, evaluate=_allow, _csrf_token="t",
        )
        await _seed_two_turns(conn, session_id, prefix="B-")
        assert _rows(conn, session_id) == [(5, 1), (6, 2), (7, 3), (8, 4)]

        # Tab A: stale truncate with the full observed precondition.
        with pytest.raises(chat_routes.HTTPException) as exc_info:
            await chat_routes.truncate_session_messages(
                _mock_request(), session_id,
                chat_routes.TruncateSessionRequest(
                    keep_seq=2, expected_tail_seq=4, expected_tail_id=4
                ),
                conn, {"id": 1}, evaluate=_allow, _csrf_token="t",
            )
        assert exc_info.value.status_code == 409
        assert _rows(conn, session_id) == [(5, 1), (6, 2), (7, 3), (8, 4)]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_aba_tail_id_proceeds_when_tail_row_matches(tmp_path):
    """A CURRENT view (expected_tail_id = the real tail row's PK) truncates
    normally — the id precondition does not over-refuse."""
    db_path = tmp_path / "aba-proceed.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        await _seed_two_turns(conn, session_id)

        result = await chat_routes.truncate_session_messages(
            _mock_request(), session_id,
            chat_routes.TruncateSessionRequest(
                keep_seq=2, expected_tail_seq=4, expected_tail_id=4
            ),
            conn, {"id": 1}, evaluate=_allow, _csrf_token="t",
        )
        assert result["remaining_count"] == 2
        assert result["tail_seq"] == 2
        assert _rows(conn, session_id) == [(1, 1), (2, 2)]
    finally:
        conn.close()
