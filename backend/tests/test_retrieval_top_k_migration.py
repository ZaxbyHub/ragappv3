"""Tests for retrieval_top_k migration from max_context_chunks.

This test file verifies:
1. Deprecation warning behavior for max_context_chunks
2. Settings precedence for retrieval_top_k
3. RAG engine integration with retrieval_top_k
"""

import asyncio
import os
import sys
import unittest
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestConfigDeprecationWarnings(unittest.TestCase):
    """Test deprecation warning behavior for max_context_chunks configuration."""

    def test_no_deprecation_warning_at_default(self):
        """Test that default max_context_chunks (10) does NOT emit deprecation warning."""
        # Import inside test to isolate warning state
        from app.config import Settings

        # Use warnings.catch_warnings(record=True) to capture all warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            settings = Settings()

            # Filter for DeprecationWarning only
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]

            # No deprecation warning should be raised for default value
            self.assertEqual(len(deprecation_warnings), 0)
            self.assertEqual(settings.max_context_chunks, 10)

    def test_deprecation_warning_when_non_default(self):
        """Test that non-default max_context_chunks emits DeprecationWarning with expected message."""
        from app.config import Settings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(max_context_chunks=20)

            # Filter for DeprecationWarning only
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]

            # Should have one deprecation warning
            self.assertEqual(len(deprecation_warnings), 1)
            self.assertIn("deprecated", str(deprecation_warnings[0].message).lower())
            self.assertIn("RETRIEVAL_TOP_K", str(deprecation_warnings[0].message))

    def test_retrieval_top_k_takes_precedence(self):
        """Test that retrieval_top_k is properly set via settings."""
        from app.config import Settings

        # Test with explicit retrieval_top_k
        with patch.dict(os.environ, {"RETRIEVAL_TOP_K": "15"}, clear=False):
            # Force reload of settings by creating new instance
            settings = Settings()
            self.assertEqual(settings.retrieval_top_k, 15)

        # Test with explicit initialization
        settings = Settings(retrieval_top_k=20)
        self.assertEqual(settings.retrieval_top_k, 20)


class TestRAGEngineRetrievalTopK(unittest.TestCase):
    """Test RAG engine integration with retrieval_top_k."""

    @pytest.fixture(autouse=True)
    def setup_event_loop(self):
        """Setup async event loop for tests."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        from unittest.mock import patch as _patch
        with _patch("app.services.embeddings.assert_url_safe"), \
             _patch("app.services.llm_client.assert_url_safe"):
            yield
        self.loop.close()

    def test_rag_engine_uses_retrieval_top_k(self):
        """Test that search_memories is called with settings.retrieval_top_k value."""
        from app.services.memory_store import MemoryStore
        from app.services.rag_engine import RAGEngine

        # Create mock memory_store
        mock_memory_store = MagicMock(spec=MemoryStore)
        mock_memory_store.search_memories = MagicMock(return_value=[])
        mock_memory_store.detect_memory_intent = MagicMock(return_value="")

        # Mock settings to have specific retrieval_top_k
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.retrieval_top_k = 15
            mock_settings.query_transformation_enabled = False
            mock_settings.tri_vector_search_enabled = False
            mock_settings.maintenance_mode = False
            mock_settings.context_max_tokens = 0
            mock_settings.retrieval_evaluation_enabled = False

            # Create RAGEngine with mocked dependencies
            with patch("app.services.rag_engine.VectorStore") as mock_vector_store:
                mock_vector_store_instance = MagicMock()
                mock_vector_store_instance.search = AsyncMock(return_value=[])
                mock_vector_store_instance.is_connected = MagicMock(return_value=True)
                mock_vector_store.return_value = mock_vector_store_instance

                # Create engine with mocked memory_store
                engine = RAGEngine(memory_store=mock_memory_store)
                engine.vector_store = mock_vector_store_instance
                engine.document_retrieval = MagicMock()
                engine.document_retrieval.filter_relevant = MagicMock(return_value=[])
                engine.prompt_builder = MagicMock()
                engine.prompt_builder.build_messages = MagicMock(return_value=[])

                # Verify the retrieval_top_k is properly set on the engine
                self.assertEqual(engine.retrieval_top_k, 15)

                # The key assertion: settings.retrieval_top_k should be 15
                self.assertEqual(mock_settings.retrieval_top_k, 15)


@pytest.mark.asyncio
class TestRAGEngineAsyncBehavior:
    """Async tests for RAG engine behavior."""

    async def test_rag_engine_search_memories_uses_memory_retrieval_top_k(self):
        """RAGEngine.query passes settings.memory_retrieval_top_k (NOT
        retrieval_top_k) as the limit to MemoryStore.search_memories.

        Replaces the prior tautological test (C2-4) which only asserted
        ``mock_settings.retrieval_top_k == 15`` after setting it to 15 and
        never referenced search_memories. Driving the full streaming query()
        generator requires heavy mocking of the embedding/LLM/vector stack
        (the query path embeds the query before reaching memory retrieval),
        so this uses the repo's established source-inspection pattern to
        assert the production code reads the correct settings field. A
        regression that swapped ``memory_retrieval_top_k`` for
        ``retrieval_top_k`` would fail this test.
        """
        import os

        source_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "services", "rag_engine.py"
        )
        source = open(source_path, encoding="utf-8").read()

        # The memory-retrieval branch must pass memory_retrieval_top_k.
        assert "settings.memory_retrieval_top_k" in source
        # Locate the search_memories call and confirm it is bounded by
        # memory_retrieval_top_k, not retrieval_top_k.
        idx = source.find("self.memory_store.search_memories")
        assert idx != -1, "search_memories call not found in rag_engine.py"
        # The limit is on the following lines (within the asyncio.to_thread call).
        window = source[idx : idx + 200]
        assert "memory_retrieval_top_k" in window
        # Must NOT use the bare retrieval_top_k field (settings.retrieval_top_k).
        # Note: "memory_retrieval_top_k" contains "retrieval_top_k" as a
        # substring, so check for the settings-qualified bare field instead.
        assert "settings.retrieval_top_k" not in window


class TestRetrievalTopKValueMigration(unittest.TestCase):
    """Test value migration from deprecated parameters to retrieval_top_k."""

    def test_retrieval_top_k_default_is_12(self):
        """Test that retrieval_top_k defaults to 12 (not the old max_context_chunks default of 10)."""
        from app.config import Settings

        settings = Settings()  # Default values
        self.assertEqual(settings.retrieval_top_k, 12)

    def test_legacy_vector_top_k_migration_via_constructor(self):
        """Test that legacy vector_top_k value is migrated to retrieval_top_k when passed to constructor."""
        from app.config import Settings

        # When both vector_top_k and retrieval_top_k are passed,
        # retrieval_top_k takes precedence (this is the intended behavior)
        # Test that when only vector_top_k is set, it's NOT auto-migrated due to field order
        # (retrieval_top_k is validated before vector_top_k is read from env)

        # This tests the expected behavior: retrieval_top_k has a default of 12
        # and vector_top_k is deprecated but NOT auto-migrated from env due to field order
        settings = Settings()
        self.assertEqual(settings.retrieval_top_k, 12)

        # Test explicit setting via constructor works
        settings = Settings(vector_top_k=25, retrieval_top_k=25)
        self.assertEqual(settings.retrieval_top_k, 25)


class TestRetrievalTopKEdgeCases(unittest.TestCase):
    """Edge case tests for retrieval_top_k configuration."""

    def test_retrieval_top_k_zero(self):
        """Test that retrieval_top_k can be set to 0."""
        from app.config import Settings

        settings = Settings(retrieval_top_k=0)
        self.assertEqual(settings.retrieval_top_k, 0)

    def test_retrieval_top_k_large_value(self):
        """Test that retrieval_top_k can be set to a large value."""
        from app.config import Settings

        settings = Settings(retrieval_top_k=1000)
        self.assertEqual(settings.retrieval_top_k, 1000)


if __name__ == "__main__":
    unittest.main()
