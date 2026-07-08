"""
Adversarial test: verify /eval/* 500 responses do not leak internal exception text.

Both POST /api/eval/ragas and POST /api/eval/live had 500 handlers that returned
``detail=f"... failed: {str(e)}"``, leaking exception text (database schema details,
API key formats, network errors) to clients.  This is the eval-route counterpart
to test_search_route_semaphore_sanitization_adversarial.py.

Fix (issue #343): sanitize the detail to a fixed "Internal server error" string
and switch from ``logger.error(..., exc_info=True)`` to ``logger.exception()``.

These tests MUST fail on pre-fix code (which interpolates ``str(e)`` into the
detail) and pass after the fix.
"""

import os
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key-for-tests")
os.environ.setdefault("USERS_ENABLED", "False")

# Stub ragas if not installed (the ragas route does `import ragas` at call time)
try:
    import ragas  # noqa: F401
except ImportError:
    sys.modules["ragas"] = types.ModuleType("ragas")

from fastapi.testclient import TestClient


class TestEvalErrorSanitization(unittest.TestCase):
    """Verify that /eval/* 500 responses return a fixed sanitized detail string."""

    @contextmanager
    def _app_with_overrides(self):
        """Yield a (TestClient, app) pair with admin auth wired up."""
        from app.api.deps import (
            get_current_active_user,
            get_embedding_service,
            get_rag_engine,
        )
        from app.main import app as main_app

        # Admin user override
        main_app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "test-admin",
            "role": "admin",
            "is_active": True,
            "must_change_password": 0,
        }
        # Embedding service mock (ragas route depends on it)
        mock_emb = MagicMock()
        mock_emb.embed_single = AsyncMock(return_value=[0.1] * 384)
        main_app.dependency_overrides[get_embedding_service] = lambda: mock_emb
        # RAG engine mock (live route depends on it)
        mock_rag = MagicMock()
        main_app.dependency_overrides[get_rag_engine] = lambda: mock_rag

        client = TestClient(main_app, raise_server_exceptions=False)
        try:
            yield client, main_app
        finally:
            main_app.dependency_overrides.clear()

    @contextmanager
    def _eval_enabled(self):
        """Patch settings.eval_enabled to True for the duration of the test."""
        with patch("app.config.settings.eval_enabled", True, create=True):
            yield

    # ---- POST /api/eval/ragas ----

    def test_ragas_error_returns_generic_500_detail(self):
        """When the RAGAS eval path raises, detail must be 'Internal server error'.

        _calculate_faithfulness is called directly in the outer try block and
        is NOT wrapped in its own try/except, so an exception here propagates
        to the route's 500 handler.
        """
        with self._eval_enabled(), self._app_with_overrides() as (client, _):
            with patch(
                "app.api.routes.eval._calculate_faithfulness",
                side_effect=Exception("API key invalid: 401 Unauthorized"),
            ):
                response = client.post(
                    "/api/eval/ragas",
                    json={
                        "query": "What is ML?",
                        "answer": "Machine learning is AI.",
                        "contexts": ["ML processes data."],
                        "ground_truth": "ML is a branch of AI.",
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["detail"], "Internal server error")

    def test_ragas_error_does_not_leak_exception_text(self):
        """Crafted exception with sensitive values must not appear in response."""
        with self._eval_enabled(), self._app_with_overrides() as (client, _):
            with patch(
                "app.api.routes.eval._calculate_faithfulness",
                side_effect=Exception("SECRET_KEY=abc123 connection refused"),
            ):
                response = client.post(
                    "/api/eval/ragas",
                    json={
                        "query": "test",
                        "answer": "answer",
                        "contexts": ["ctx"],
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        detail = response.json()["detail"]
        self.assertNotIn("SECRET_KEY", detail)
        self.assertNotIn("abc123", detail)
        self.assertNotIn("connection", detail)
        self.assertNotIn("refused", detail)

    # ---- POST /api/eval/live ----

    def test_live_error_returns_generic_500_detail(self):
        """When the live eval path raises, detail must be 'Internal server error'."""
        with self._eval_enabled(), self._app_with_overrides() as (client, _):
            with patch(
                "app.services.eval_adapter.LiveEvalAdapter.run_live",
                new_callable=AsyncMock,
                side_effect=Exception("Database connection failed: timeout"),
            ):
                response = client.post(
                    "/api/eval/live",
                    json={
                        "benchmark": [
                            {
                                "id": "q1",
                                "query": "test query",
                                "relevant_ids": ["doc1"],
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["detail"], "Internal server error")

    def test_live_error_does_not_leak_exception_text(self):
        """Crafted exception with sensitive values must not appear in response."""
        with self._eval_enabled(), self._app_with_overrides() as (client, _):
            with patch(
                "app.services.eval_adapter.LiveEvalAdapter.run_live",
                new_callable=AsyncMock,
                side_effect=Exception(
                    "psycopg2.OperationalError: password=sup3rs3cret"
                ),
            ):
                response = client.post(
                    "/api/eval/live",
                    json={
                        "benchmark": [
                            {
                                "id": "q1",
                                "query": "test query",
                                "relevant_ids": ["doc1"],
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 500, response.text)
        detail = response.json()["detail"]
        self.assertNotIn("psycopg2", detail)
        self.assertNotIn("password", detail)
        self.assertNotIn("sup3rs3cret", detail)

    def test_both_endpoints_return_exactly_fixed_string_with_adversarial_payload(
        self,
    ):
        """Even with adversarial exception payloads, the fixed string must hold."""
        # Ragas
        with self._eval_enabled(), self._app_with_overrides() as (client, _):
            with patch(
                "app.api.routes.eval._calculate_faithfulness",
                side_effect=Exception("concurrency=999 acquisition timed out"),
            ):
                resp_ragas = client.post(
                    "/api/eval/ragas",
                    json={"query": "x", "answer": "y", "contexts": ["z"]},
                )

        self.assertEqual(resp_ragas.status_code, 500)
        detail_ragas = resp_ragas.json()["detail"]
        self.assertEqual(detail_ragas, "Internal server error")
        self.assertNotIn("999", detail_ragas)
        self.assertNotIn("acquisition", detail_ragas.lower())

        # Live
        with self._eval_enabled(), self._app_with_overrides() as (client, _):
            with patch(
                "app.services.eval_adapter.LiveEvalAdapter.run_live",
                new_callable=AsyncMock,
                side_effect=Exception("concurrency=999 acquisition timed out"),
            ):
                resp_live = client.post(
                    "/api/eval/live",
                    json={
                        "benchmark": [
                            {"id": "q", "query": "q", "relevant_ids": []}
                        ]
                    },
                )

        self.assertEqual(resp_live.status_code, 500)
        detail_live = resp_live.json()["detail"]
        self.assertEqual(detail_live, "Internal server error")
        self.assertNotIn("999", detail_live)
        self.assertNotIn("acquisition", detail_live.lower())


if __name__ == "__main__":
    unittest.main()
