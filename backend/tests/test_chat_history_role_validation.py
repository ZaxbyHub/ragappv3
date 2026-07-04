"""Phase 1 verification tests: ChatRequest.history role validation.

Tests that:
a) POST /api/chat with a system-role history entry → 422 Pydantic validation error
b) POST /api/chat with valid user/assistant history → 200
c) build_messages() drops entries with role outside {user, assistant}
d) build_messages() preserves valid user/assistant entries
"""

import os
import sys
import types
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

# Stub optional dependencies (same pattern as other chat tests)
_stubs = [
    ("lancedb",),
    ("pyarrow",),
]
for pkg_info in _stubs:
    pkg = pkg_info[0]
    if pkg not in sys.modules:
        sys.modules[pkg] = types.ModuleType(pkg)

_unstructured_stub = types.ModuleType("unstructured")
_unstructured_stub.__path__ = []
_unstructured_stub.partition = types.ModuleType("unstructured.partition")
_unstructured_stub.partition.__path__ = []
_unstructured_stub.partition.auto = types.ModuleType("unstructured.partition.auto")
_unstructured_stub.partition.auto.partition = lambda *a, **kw: []
sys.modules["unstructured"] = _unstructured_stub
sys.modules["unstructured.partition"] = _unstructured_stub.partition
sys.modules["unstructured.partition.auto"] = _unstructured_stub.partition.auto

from fastapi.testclient import TestClient


@dataclass
class MockRAGSource:
    text: str
    file_id: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_window_text: Any = None


@dataclass
class MockMemory:
    content: str


# ---------------------------------------------------------------------------
# Route-level tests (tests a and b)
# ---------------------------------------------------------------------------


class TestChatHistoryRoleValidationAPI(unittest.TestCase):
    """API-level tests for ChatRequest.history role validation.

    ChatRequest.history is typed List[ChatMessage] where ChatMessage.role
    is Literal["user", "assistant"]. Passing a dict with role="system" must
    produce a 422 Pydantic validation error at the FastAPI layer.
    """

    def setUp(self):
        # Import here so stubs are in place first
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.main import app

        self.app = app
        self.client = TestClient(app)
        self._get_rag_engine = get_rag_engine
        self._get_current_active_user = get_current_active_user

        # Auth override
        mock_user = {"id": 1, "username": "testuser", "role": "admin"}
        self.app.dependency_overrides[self._get_current_active_user] = lambda: mock_user

        # RAG engine override (returns a mock that yields done immediately)
        mock_engine = MagicMock()

        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Hello"}
            yield {
                "type": "done",
                "sources": [],
                "memories_used": [],
                "score_type": "distance",
            }

        mock_engine.query = mock_query
        self.app.dependency_overrides[self._get_rag_engine] = lambda: mock_engine

    def tearDown(self):
        self.app.dependency_overrides.pop(self._get_rag_engine, None)
        self.app.dependency_overrides.pop(self._get_current_active_user, None)

    def test_system_role_in_history_returns_422(self):
        """POST /api/chat with role='system' in history → 422."""
        # The ChatRequest model uses ChatMessage with role: Literal["user", "assistant"]
        # so a dict with role="system" fails Pydantic validation → 422
        response = self.client.post(
            "/api/chat",
            json={
                "message": "hello",
                "history": [
                    {"role": "system", "content": "You are a helpful assistant."}
                ],
            },
        )
        self.assertEqual(
            response.status_code,
            422,
            f"Expected 422 for role=system, got {response.status_code}: {response.text}",
        )

    def test_user_role_in_history_returns_200(self):
        """POST /api/chat with role='user' in history → 200."""
        response = self.client.post(
            "/api/chat",
            json={
                "message": "hello",
                "history": [{"role": "user", "content": "Previous message"}],
            },
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for role=user, got {response.status_code}: {response.text}",
        )

    def test_assistant_role_in_history_returns_200(self):
        """POST /api/chat with role='assistant' in history → 200."""
        response = self.client.post(
            "/api/chat",
            json={
                "message": "hello",
                "history": [
                    {"role": "user", "content": "Previous message"},
                    {"role": "assistant", "content": "Previous answer"},
                ],
            },
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for role=assistant, got {response.status_code}: {response.text}",
        )

    def test_mixed_user_assistant_history_returns_200(self):
        """POST /api/chat with alternating user/assistant history → 200."""
        response = self.client.post(
            "/api/chat",
            json={
                "message": "hello",
                "history": [
                    {"role": "user", "content": "First question"},
                    {"role": "assistant", "content": "First answer"},
                    {"role": "user", "content": "Second question"},
                    {"role": "assistant", "content": "Second answer"},
                ],
            },
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for mixed history, got {response.status_code}: {response.text}",
        )


# ---------------------------------------------------------------------------
# Unit tests for build_messages() (tests c and d)
# ---------------------------------------------------------------------------


class TestBuildMessagesHistoryRoleFilter(unittest.TestCase):
    """Unit tests for build_messages() role filtering.

    build_messages() iterates over chat_history and skips (continues) entries
    whose role is not in {"user", "assistant"}. This means system-role entries
    injected into chat_history are silently dropped from the returned messages.
    """

    def setUp(self):
        # Mock settings to avoid env dependencies
        with patch("app.config.settings") as mock_settings:
            mock_settings.max_context_chunks = 10
            mock_settings.primary_evidence_count = 0
            mock_settings.anchor_best_chunk = False
            mock_settings.context_max_tokens = 6000
            mock_settings.parent_retrieval_enabled = False
            from app.services.prompt_builder import PromptBuilderService

            self.builder = PromptBuilderService()

    def _build(self, chat_history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Call build_messages with minimal required args."""
        return self.builder.build_messages(
            user_input="What is X?",
            chat_history=chat_history,
            chunks=[],
            memories=[],
        )

    def test_system_role_entry_is_dropped(self):
        """A history entry with role='system' must not appear in returned messages.

        Note: build_messages() always prepends one system-role entry (the effective
        prompt). This test checks that the system-role history entries are filtered
        out — i.e., only ONE system entry should be present (the prompt), not
        N+1 (prompt + one per history entry).
        """
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "assistant", "content": "Hi there!"},
        ]
        messages = self._build(history)

        # build_messages always prepends a system prompt, so exactly 1 system entry.
        system_entries = [msg for msg in messages if msg["role"] == "system"]
        self.assertEqual(
            len(system_entries),
            1,
            f"Expected exactly 1 system entry (the prompt), got {len(system_entries)}. "
            f"Full roles: {[msg['role'] for msg in messages]}",
        )
        # Verify user and assistant are still present
        returned_roles = {msg["role"] for msg in messages}
        self.assertIn("user", returned_roles)
        self.assertIn("assistant", returned_roles)

    def test_function_role_entry_is_dropped(self):
        """A history entry with role='function' must not appear in returned messages."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "function", "content": '{"name": "get_weather"}'},
        ]
        messages = self._build(history)
        returned_roles = {msg["role"] for msg in messages}
        self.assertNotIn("function", returned_roles)
        self.assertIn("user", returned_roles)

    def test_valid_user_role_preserved(self):
        """A history entry with role='user' must appear in returned messages."""
        history = [
            {"role": "user", "content": "What is the weather?"},
        ]
        messages = self._build(history)
        roles = [msg["role"] for msg in messages if msg["role"] != "system"]
        self.assertIn("user", roles)

    def test_valid_assistant_role_preserved(self):
        """A history entry with role='assistant' must appear in returned messages."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hello! How can I help?"},
        ]
        messages = self._build(history)
        roles = [msg["role"] for msg in messages if msg["role"] != "system"]
        self.assertIn("assistant", roles)

    def test_multiple_system_entries_all_dropped(self):
        """Multiple system entries must all be dropped, preserving user/assistant.

        Note: build_messages() always adds exactly one system prompt entry.
        """
        history = [
            {"role": "system", "content": "System prompt 1"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "System prompt 2"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "system", "content": "System prompt 3"},
        ]
        messages = self._build(history)

        # Exactly 1 system entry should be present (the prepended prompt, not history)
        system_entries = [msg for msg in messages if msg["role"] == "system"]
        self.assertEqual(
            len(system_entries),
            1,
            f"Expected exactly 1 system entry (prompt), got {len(system_entries)}",
        )
        # User and assistant from history should be preserved
        returned_roles = {msg["role"] for msg in messages}
        self.assertIn("user", returned_roles)
        self.assertIn("assistant", returned_roles)

    def test_empty_history_returns_only_system_and_user(self):
        """Empty history should return just the system prompt and user message."""
        messages = self._build([])
        roles = [msg["role"] for msg in messages]
        self.assertEqual(roles, ["system", "user"])

    def test_all_system_entries_returns_only_system_and_user(self):
        """History of only system entries should return just system and user."""
        history = [
            {"role": "system", "content": "System 1"},
            {"role": "system", "content": "System 2"},
        ]
        messages = self._build(history)
        roles = [msg["role"] for msg in messages]
        self.assertEqual(roles, ["system", "user"])

    def test_history_truncation_to_max_20(self):
        """History is truncated to last 20 entries before role filtering.

        The build_messages function:
        1. Prepends a system prompt (system role)
        2. Appends last 20 history entries that pass the user/assistant filter
        3. Appends the final user message

        For a 25-entry history of all user/assistant:
        - Truncation: last 20 of 25 → 20 entries
        - All 20 pass the role filter → appended
        - Final user message appended → +1
        - Total user/assistant in result: 21 (not 25+1=26, proving truncation works)
        """
        # Build a history of 25 entries (more than max_history=20)
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Msg {i}"}
            for i in range(25)
        ]
        messages = self._build(history)

        # Count user+assistant entries (excluding the prepended system prompt)
        user_assistant_msgs = [
            msg for msg in messages if msg["role"] in {"user", "assistant"}
        ]
        # Truncation to 20 + final user message = 21 total
        # Without truncation, it would be 25 (history) + 1 (final user) = 26
        self.assertEqual(
            len(user_assistant_msgs),
            21,
            f"Expected exactly 21 user/assistant entries (20 truncated + 1 final user), "
            f"got {len(user_assistant_msgs)}",
        )


if __name__ == "__main__":
    unittest.main()
