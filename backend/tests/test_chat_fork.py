import json
import os
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes import chat as chat_routes
from app.models.database import init_db


def _mock_request():
    request = MagicMock(spec=Request)
    request.client.host = "127.0.0.1"
    return request


class FailingMessageCopyConnection(sqlite3.Connection):
    fail_message_copy = False
    fail_after_message_copies = 0
    copied_message_count = 0

    def execute(self, sql, parameters=(), /):
        if self.fail_message_copy and sql.lstrip().upper().startswith(
            "INSERT INTO CHAT_MESSAGES"
        ):
            self.copied_message_count += 1
            if self.copied_message_count > self.fail_after_message_copies:
                raise sqlite3.OperationalError("forced copy failure")
        return super().execute(sql, parameters)


@pytest.mark.asyncio
async def test_fork_response_returns_inserted_message_ids_and_created_at(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "fork-success.db"
    init_db(str(db_path))
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        source_session_id = conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
            (1, 1, "Original"),
        ).lastrowid
        source_message_id = conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
            (source_session_id, "user", "Question", None),
        ).lastrowid
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
            (source_session_id, "assistant", "Answer", None),
        )
        conn.commit()

        async def allow_write(*args):
            return True

        monkeypatch.setattr(chat_routes, "get_evaluate_policy", lambda conn, _ep=allow_write: _ep)

        response = await chat_routes.fork_session(
            _mock_request(),
            source_session_id,
            chat_routes.ForkSessionRequest(message_index=1),
            conn,
            {"id": 1},
        )

        assert response["forked_from_session_id"] == source_session_id
        assert [message["content"] for message in response["messages"]] == [
            "Question",
            "Answer",
        ]
        assert all(isinstance(message["id"], int) for message in response["messages"])
        assert response["messages"][0]["id"] != source_message_id
        assert all(message["created_at"] for message in response["messages"])
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_copies_and_returns_wiki_refs_when_column_exists(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "fork-wiki-refs.db"
    init_db(str(db_path))
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN wiki_refs TEXT")
        source_session_id = conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
            (1, 1, "Original"),
        ).lastrowid
        wiki_refs = [{"wiki_label": "W1", "title": "Runbook"}]
        memories = [{"memory_label": "M1", "content": "Known fact"}]
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources, memories, wiki_refs) VALUES (?, ?, ?, ?, ?, ?)",
            (
                source_session_id,
                "assistant",
                "Answer with [W1]",
                None,
                json.dumps(memories),
                json.dumps(wiki_refs),
            ),
        )
        conn.commit()

        async def allow_write(*args):
            return True

        monkeypatch.setattr(chat_routes, "get_evaluate_policy", lambda conn, _ep=allow_write: _ep)

        response = await chat_routes.fork_session(
            _mock_request(),
            source_session_id,
            chat_routes.ForkSessionRequest(message_index=0),
            conn,
            {"id": 1},
        )

        forked_session_id = response["id"]
        copied_wiki_refs = conn.execute(
            "SELECT wiki_refs FROM chat_messages WHERE session_id = ?",
            (forked_session_id,),
        ).fetchone()[0]
        assert json.loads(copied_wiki_refs) == wiki_refs
        assert response["messages"][0]["memories"] == memories
        assert response["messages"][0]["wiki_refs"] == wiki_refs
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fork_rolls_back_session_when_message_copy_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "fork-atomicity.db"
    init_db(str(db_path))
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        factory=FailingMessageCopyConnection,
    )
    try:
        source_session_id = conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
            (1, 1, "Original"),
        ).lastrowid
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
            (source_session_id, "user", "Question", None),
        )
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
            (source_session_id, "assistant", "Answer", None),
        )
        conn.commit()

        async def allow_write(*args):
            return True

        monkeypatch.setattr(chat_routes, "get_evaluate_policy", lambda conn, _ep=allow_write: _ep)
        conn.fail_message_copy = True
        conn.fail_after_message_copies = 1

        with pytest.raises(sqlite3.OperationalError, match="forced copy failure"):
            await chat_routes.fork_session(
                _mock_request(),
                source_session_id,
                chat_routes.ForkSessionRequest(message_index=1),
                conn,
                {"id": 1},
            )

        branch_count = conn.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE forked_from_session_id = ?",
            (source_session_id,),
        ).fetchone()[0]
        assert branch_count == 0
    finally:
        conn.close()
