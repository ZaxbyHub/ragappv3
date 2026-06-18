"""
Adversarial test: verify search route 503 response does not leak internal state.

SearchSemaphoreTimeoutError raised by _acquire_search_semaphore contains
internal semaphore state (concurrency=N) in its message.  The search route
handler must NOT pass str(e) to the HTTPException detail — it must return a
fixed, sanitized string.

This is the search-route counterpart to
test_chat_search_semaphore_timeout.py::test_search_semaphore_timeout_does_not_leak_internal_details.
"""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure test env so imported modules don't bail on missing env vars
os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key-for-tests")
os.environ.setdefault("USERS_ENABLED", "False")

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.services.vector_store import SearchSemaphoreTimeoutError


class _FakeEmbeddingService:
    """Fake embedding service that returns a fixed-size embedding vector."""

    def __init__(self, dim: int = 384):
        self.dim = dim

    async def embed_single(self, text: str):
        """Return a deterministic fake embedding."""
        # Deterministic fake embedding: deterministic seed based on text
        import hashlib

        h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        return [float((h >> (i % 64)) & 1) * 0.1 for i in range(self.dim)]


class _FakeVectorStore:
    """Fake vector store whose search raises SearchSemaphoreTimeoutError."""

    def __init__(self, error: Exception):
        self._error = error

    async def init_table(self, embedding_dim: int) -> None:
        pass

    async def search(self, embedding, limit: int, vault_id=None):
        raise self._error


class TestSearchRouteSemaphoreSanitization(unittest.TestCase):
    """Adversarial: ensure 503 response is sanitized and does not leak semaphore internals."""

    @contextmanager
    def _app_with_overrides(self, error: Exception):
        """Build a test client with vector store overridden to raise `error`."""
        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_embedding_service,
            get_evaluate_policy,
            get_vector_store,
        )
        from app.main import app as main_app

        fake_vs = _FakeVectorStore(error)
        fake_emb = _FakeEmbeddingService(dim=384)

        main_app.dependency_overrides[get_vector_store] = lambda: fake_vs
        main_app.dependency_overrides[get_embedding_service] = lambda: fake_emb
        main_app.dependency_overrides[get_db] = self._get_test_db
        main_app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "test-admin",
            "role": "admin",
        }

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        main_app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy

        client = TestClient(main_app, raise_server_exceptions=False)
        try:
            yield client
        finally:
            main_app.dependency_overrides.clear()

    def _get_test_db(self):
        """Yield a mock DB connection."""
        conn = MagicMock()
        conn.cursor.return_value = MagicMock()
        yield conn

    def test_503_detail_is_fixed_string_not_exception_message(self):
        """
        When VectorStore raises SearchSemaphoreTimeoutError, the HTTP 503
        detail must be exactly 'Search temporarily unavailable' — not the
        raw exception message, not a formatted string containing semaphore state.
        """
        # The internal exception message includes sensitive state
        internal_error = SearchSemaphoreTimeoutError(
            "Search semaphore acquisition timed out after 30.0s; concurrency=32"
        )

        with self._app_with_overrides(internal_error) as client:
            response = client.post(
                "/api/search",
                json={"query": "test query", "limit": 5},
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"], "Search temporarily unavailable")

    def test_503_detail_does_not_contain_concurrency_value(self):
        """
        The 503 response detail must not contain 'concurrency=' or the
        semaphore _value, which is internal asyncio implementation state.
        """
        internal_error = SearchSemaphoreTimeoutError(
            "Search semaphore acquisition timed out after 30.0s; concurrency=32"
        )

        with self._app_with_overrides(internal_error) as client:
            response = client.post(
                "/api/search",
                json={"query": "test query", "limit": 5},
            )

        self.assertEqual(response.status_code, 503, response.text)
        detail = response.json()["detail"]
        self.assertNotIn("concurrency", detail)
        self.assertNotIn("30.0", detail)
        self.assertNotIn("semaphore", detail.lower())
        self.assertNotIn("acquisition timed out", detail.lower())
        self.assertNotIn("Search semaphore", detail)

    def test_503_detail_is_exactly_fixed_string_with_adversarial_payload(self):
        """
        Even if the error message contains crafted values (e.g. 999),
        the fixed sanitized string must still be returned.
        """
        internal_error = SearchSemaphoreTimeoutError(
            "Search semaphore acquisition timed out after 999.0s; concurrency=999"
        )

        with self._app_with_overrides(internal_error) as client:
            response = client.post(
                "/api/search",
                json={"query": "adversarial payload attempt", "limit": 5},
            )

        self.assertEqual(response.status_code, 503, response.text)
        # Must be EXACTLY this string — not interpolated or reformatted
        self.assertEqual(response.json()["detail"], "Search temporarily unavailable")
        # Sanity: the adversarial values must NOT appear
        self.assertNotIn("999", response.json()["detail"])
        self.assertNotIn("acquisition", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
