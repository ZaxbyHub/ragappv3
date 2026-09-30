"""Issue #684 (C1): a stale-view truncate must refuse instead of deleting.

The truncate endpoint (CHAT-006) currently deletes ``seq > boundary`` with NO
precondition on the session's current tail. The contract this suite encodes:
the client supplies ``expected_tail_seq`` — the tail seq its view was built
from — and when the server's actual tail is NEWER (another tab/device saved
rows the client never saw), the endpoint must refuse with HTTP 409 and delete
NOTHING. A stale client must never delete rows it never saw.

At the pre-fix tree this is RED: ``expected_tail_seq`` is silently dropped by
pydantic (unknown kwarg on ``TruncateSessionRequest``), the handler trims at
``keep_seq=0`` and every row is gone (``assert 0 == 4``).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes import chat as chat_routes
from app.models.database import init_db, run_migrations
from tests.test_chat_turns import _allow, _connect, _make_session, _mock_request, _msg


@pytest.mark.asyncio
async def test_stale_view_truncate_deletes_nothing(tmp_path):
    db_path = tmp_path / "a02-stale-view.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    try:
        session_id = _make_session(conn)
        conn.commit()
        # Two durable turns: rows carry seq 1..4, so the session's actual tail
        # is seq 4.
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

        # The client's view was built when the tail was seq 2 (turn 1 only);
        # turn 2's rows (seq 3..4) are unseen by it. It asks to clear the
        # session (keep_seq=0) while declaring its expected tail — the server
        # must observe the tail moved on and refuse.
        status = None
        try:
            await chat_routes.truncate_session_messages(
                _mock_request(),
                session_id,
                chat_routes.TruncateSessionRequest(keep_seq=0, expected_tail_seq=2),
                conn,
                {"id": 1},
                evaluate=_allow,
                _csrf_token="t",
            )
        except chat_routes.HTTPException as exc:
            status = exc.status_code

        # FIRST: the stale view must not have deleted the unseen rows...
        remaining_rows = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        assert remaining_rows == 4
        # ...and ONLY THEN: the refusal is the 409 stale-view conflict.
        assert status == 409
    finally:
        conn.close()
