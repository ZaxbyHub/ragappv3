"""Tests for startup model assertion in lifespan.py."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestStartupModelAssertion:
    """Tests for TEI model assertion at startup."""

    def test_strict_embedding_model_check_config_exists(self):
        """strict_embedding_model_check config should exist in settings."""
        from app.config import settings
        assert hasattr(settings, 'strict_embedding_model_check'), "strict_embedding_model_check config should exist"
        # Default should be True
        assert settings.strict_embedding_model_check

    def test_strict_embedding_model_check_default_is_true(self):
        """strict_embedding_model_check should default to True."""
        from app.config import settings
        assert settings.strict_embedding_model_check

    def test_lifespan_has_model_validation_code(self):
        """lifespan.py should have model validation code."""
        # Read the file directly to avoid pyarrow import issues
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()
        assert 'strict_embedding_model_check' in source, "lifespan should reference strict_embedding_model_check"
        assert '/info' in source, "lifespan should call TEI /info endpoint"
        assert 'model_id' in source, "lifespan should check model_id"
        assert 'RuntimeError' in source, "lifespan should raise RuntimeError on mismatch"

    def test_lifespan_restores_instant_chat_settings(self):
        """Startup persisted-settings restore should include instant mode fields."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()
        for key in [
            "ollama_chat_url",
            "instant_chat_url",
            "instant_chat_model",
            "default_chat_mode",
            "instant_initial_retrieval_top_k",
            "instant_reranker_top_n",
            "instant_memory_context_top_k",
            "instant_max_tokens",
            "thinking_max_tokens",
        ]:
            assert key in source, f"lifespan should restore persisted {key}"
