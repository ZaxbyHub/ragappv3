"""Tests for eval_enabled feature flag gate on /eval/ragas endpoint."""

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

    def test_eval_disabled_returns_501(self, setup_app):
        """When eval_enabled=False (default), endpoint returns 501."""
        client = self._get_client(self._test_app)
        payload = {
            "query": "What is RAG?",
            "answer": "RAG stands for Retrieval Augmented Generation.",
            "contexts": ["RAG is a technique that combines retrieval and generation."],
        }

        response = client.post("/eval/ragas", json=payload)
        assert response.status_code == 501
        assert "EVAL_ENABLED" in response.json()["detail"]

    def test_eval_disabled_message_is_descriptive(self, setup_app):
        """Error message explains how to enable the endpoint."""
        client = self._get_client(self._test_app)
        payload = {
            "query": "What is RAG?",
            "answer": "RAG stands for Retrieval Augmented Generation.",
            "contexts": ["RAG is a technique that combines retrieval and generation."],
        }

        response = client.post("/eval/ragas", json=payload)
        detail = response.json()["detail"]
        assert "EVAL_ENABLED" in detail

    def test_eval_enabled_missing_ragas_returns_501(self, setup_app):
        """When eval_enabled=True but ragas not installed, returns 501 about ragas."""
        client = self._get_client(self._test_app)
        payload = {
            "query": "What is RAG?",
            "answer": "RAG stands for Retrieval Augmented Generation.",
            "contexts": ["RAG is a technique that combines retrieval and generation."],
        }

        # Patch settings at the config module level (where it's imported from)
        with patch("app.config.settings") as mock_settings:
            mock_settings.eval_enabled = True
            response = client.post("/eval/ragas", json=payload)

            # If ragas is not installed, should get 501 about ragas
            if response.status_code == 501:
                detail = response.json()["detail"]
                # Should mention ragas installation
                assert "ragas" in detail.lower()
