"""Tests for the eval_enabled feature flag gate on the evaluation endpoints.

The gate applies solely to ``settings.eval_enabled`` (the historical
``import ragas`` install-presence gate was removed in issue #283). The
canonical heuristic route is ``/eval/heuristic``; ``/eval/ragas`` is a
deprecated alias gated identically (issue #343 / #237).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestEvalFeatureGate:
    """Tests for the eval_enabled feature flag gate."""

    @pytest.fixture(autouse=True)
    def setup_app(self):
        """Set up test app with mocked dependencies."""
        from app.api.routes.eval import router

        test_app = FastAPI()
        test_app.include_router(router)

        # Mock the embedding service dependency
        mock_service = MagicMock()
        mock_service.embed_single = AsyncMock(return_value=[0.1] * 384)
        test_app.dependency_overrides = {}

        # Store for use in tests
        self._test_app = test_app
        self._mock_service = mock_service
        self._router = router

        yield

        test_app.dependency_overrides.clear()

    def _get_client(self, app):
        """Get test client with mocked embedding service and auth."""
        from app.api.deps import get_embedding_service, require_admin_role

        app.dependency_overrides[get_embedding_service] = lambda: self._mock_service
        app.dependency_overrides[require_admin_role] = lambda: {
            "id": 1, "username": "admin", "role": "admin", "is_active": True
        }
        return TestClient(app)

    @staticmethod
    def _payload():
        return {
            "query": "What is RAG?",
            "answer": "RAG stands for Retrieval Augmented Generation.",
            "contexts": ["RAG is a technique that combines retrieval and generation."],
        }

    def test_eval_disabled_returns_501_on_canonical_route(self, setup_app):
        """When eval_enabled=False (default), the canonical route returns 501."""
        client = self._get_client(self._test_app)

        response = client.post("/eval/heuristic", json=self._payload())
        assert response.status_code == 501
        assert "EVAL_ENABLED" in response.json()["detail"]

    def test_eval_disabled_returns_501_on_deprecated_alias(self, setup_app):
        """The deprecated /eval/ragas alias is gated identically."""
        client = self._get_client(self._test_app)

        response = client.post("/eval/ragas", json=self._payload())
        assert response.status_code == 501
        assert "EVAL_ENABLED" in response.json()["detail"]

    def test_eval_disabled_message_is_descriptive(self, setup_app):
        """Error message explains how to enable the endpoint."""
        client = self._get_client(self._test_app)

        response = client.post("/eval/heuristic", json=self._payload())
        detail = response.json()["detail"]
        assert "EVAL_ENABLED" in detail

    def test_eval_enabled_returns_200_on_canonical_route(self, setup_app):
        """When eval_enabled=True, the canonical route serves the request."""
        client = self._get_client(self._test_app)

        with patch("app.config.settings.eval_enabled", True, create=True):
            response = client.post("/eval/heuristic", json=self._payload())

        assert response.status_code == 200
        assert "metrics" in response.json()
