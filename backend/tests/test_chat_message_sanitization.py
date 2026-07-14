"""Test that chat_messages content is sanitized at the persistence boundary."""

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSanitizeOnPersist(unittest.TestCase):
    def setUp(self):
        # Lazy imports inside setUp so test discovery works even if
        # external deps fail: this test exercises the route logic directly
        # via an in-memory db rather than spinning up the full FastAPI app.
        from app.api.routes import chat as chat_module

        self.chat_module = chat_module

        self.db_path = tempfile.mkstemp(suffix=".db")[1]
        from app.models.database import init_db, run_migrations

        init_db(self.db_path)
        run_migrations(self.db_path)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "INSERT INTO chat_sessions (id, vault_id, title) VALUES (1, 1, 'Test')"
        )
        self.conn.commit()

        # These tests focus on sanitization behaviour, not authorization. The
        # route's policy check is satisfied by passing an allow-all evaluate
        # callable directly (the route now takes evaluate via DI, so direct
        # calls receive it as a parameter rather than via module patching).
        self._allow_evaluate = AsyncMock(return_value=True)

        self.mock_request = MagicMock(spec=Request)
        self.mock_request.client.host = "127.0.0.1"

    def tearDown(self):
        pass
        try:
            self.conn.close()
        finally:
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_assistant_content_with_thinking_is_sanitized(self):
        from app.api.routes.chat import AddMessageRequest, add_message

        req = AddMessageRequest(
            role="assistant",
            content="<think>private plan</think>Final answer with [S1].",
        )
        # add_message is async; run it via asyncio.
        # Stub the rag_engine dep to None so the route exercises the no-LLM path.
        result = asyncio.run(
            add_message(
                session_id=1,
                request=self.mock_request,
                body=req,
                conn=self.conn,
                user={"id": 1, "username": "u", "role": "admin"},
                rag_engine=None,
                evaluate=self._allow_evaluate,
            )
        )

        self.assertEqual(result["content"], "Final answer with [S1].")
        self.assertNotIn("private plan", result["content"])

        # Verify the on-disk row is also clean.
        row = self.conn.execute(
            "SELECT content FROM chat_messages WHERE id = ?", (result["id"],)
        ).fetchone()
        self.assertEqual(row["content"], "Final answer with [S1].")

    def test_user_content_is_not_sanitized(self):
        # User content is kept as-is — users may legitimately type "<think>"
        # text or quote prior assistant text.
        from app.api.routes.chat import AddMessageRequest, add_message

        req = AddMessageRequest(
            role="user", content="please ignore <think>not assistant text</think>"
        )
        result = asyncio.run(
            add_message(
                session_id=1,
                request=self.mock_request,
                body=req,
                conn=self.conn,
                user={"id": 1, "username": "u", "role": "admin"},
                rag_engine=None,
                evaluate=self._allow_evaluate,
            )
        )
        # User content is preserved verbatim.
        self.assertIn("<think>", result["content"])

    def test_memories_persisted_and_returned(self):
        from app.api.routes.chat import AddMessageRequest, add_message

        memories = [
            {
                "id": "1",
                "memory_label": "M1",
                "content": "User likes concise reports.",
                "category": None,
                "tags": None,
                "source": None,
                "vault_id": 1,
                "score": None,
                "score_type": None,
                "created_at": None,
                "updated_at": None,
            }
        ]
        req = AddMessageRequest(
            role="assistant",
            content="Per [M1], here is a concise summary.",
            memories=memories,
        )
        result = asyncio.run(
            add_message(
                session_id=1,
                request=self.mock_request,
                body=req,
                conn=self.conn,
                user={"id": 1, "username": "u", "role": "admin"},
                rag_engine=None,
                evaluate=self._allow_evaluate,
            )
        )
        self.assertIsNotNone(result.get("memories"))
        self.assertEqual(len(result["memories"]), 1)
        self.assertEqual(result["memories"][0]["memory_label"], "M1")


if __name__ == "__main__":
    unittest.main()
