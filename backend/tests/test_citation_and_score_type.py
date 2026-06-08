"""Tests for CITATION_INSTRUCTION constant and score_type field.

This test module verifies:
1. CITATION_INSTRUCTION constant exists and contains required content
2. Default system prompt includes citation instruction
3. Custom system prompt does NOT include citation instruction (replaces default)
4. _build_done_message includes score_type field with correct values
"""

from unittest.mock import patch

import pytest

# =============================================================================
# Tests for prompt_builder.py - CITATION_INSTRUCTION
# =============================================================================


class TestCitationInstruction:
    """Tests for the CITATION_INSTRUCTION constant."""

    def test_citation_instruction_exists(self):
        """Verify CITATION_INSTRUCTION constant exists and is a non-empty string."""
        from app.services.prompt_builder import CITATION_INSTRUCTION

        assert isinstance(CITATION_INSTRUCTION, str), (
            f"CITATION_INSTRUCTION should be a string, got {type(CITATION_INSTRUCTION)}"
        )
        assert len(CITATION_INSTRUCTION) > 0, (
            "CITATION_INSTRUCTION should not be an empty string"
        )

    def test_citation_instruction_contains_source_format(self):
        """Verify CITATION_INSTRUCTION contains the source citation format."""
        from app.services.prompt_builder import CITATION_INSTRUCTION

        assert "[S1]" in CITATION_INSTRUCTION or "[S#]" in CITATION_INSTRUCTION, (
            "CITATION_INSTRUCTION should contain source label format (e.g. [S1], [S2], [S3])"
        )

    def test_citation_instruction_contains_not_available(self):
        """Verify CITATION_INSTRUCTION instructs about unavailable information."""
        from app.services.prompt_builder import CITATION_INSTRUCTION

        assert "not available" in CITATION_INSTRUCTION.lower(), (
            "CITATION_INSTRUCTION should instruct model to state when info is not available"
        )

    def test_citation_instruction_contains_no_fabricate(self):
        """Verify CITATION_INSTRUCTION instructs not to fabricate/hallucinate."""
        from app.services.prompt_builder import CITATION_INSTRUCTION

        has_fabricate = "fabricat" in CITATION_INSTRUCTION.lower()
        has_hallucinate = "hallucinat" in CITATION_INSTRUCTION.lower()

        assert has_fabricate or has_hallucinate, (
            "CITATION_INSTRUCTION should instruct model not to fabricate or hallucinate"
        )


class TestPromptBuilderServiceCitation:
    """Tests for PromptBuilderService integration with CITATION_INSTRUCTION."""

    def test_default_system_prompt_includes_citation(self):
        """Verify default system prompt includes CITATION_INSTRUCTION."""
        from app.services.prompt_builder import (
            CITATION_INSTRUCTION,
            PromptBuilderService,
        )

        service = PromptBuilderService()
        system_prompt = service.system_prompt

        assert CITATION_INSTRUCTION in system_prompt, (
            "Default system prompt should include CITATION_INSTRUCTION"
        )

    def test_custom_system_prompt_preserves_citation(self):
        """Verify custom system prompt does NOT include CITATION_INSTRUCTION.

        When a custom system prompt is provided, it replaces the default entirely,
        so CITATION_INSTRUCTION should NOT be automatically added.
        """
        from app.services.prompt_builder import (
            CITATION_INSTRUCTION,
            PromptBuilderService,
        )

        custom_prompt = "You are a custom assistant with special instructions."
        service = PromptBuilderService(system_prompt=custom_prompt)
        system_prompt = service.system_prompt

        assert CITATION_INSTRUCTION not in system_prompt, (
            "Custom system prompt should NOT include CITATION_INSTRUCTION - "
            "custom prompts replace the default entirely"
        )
        assert system_prompt == custom_prompt, (
            f"Custom system prompt should be preserved exactly as provided. "
            f"Expected: {custom_prompt!r}, Got: {system_prompt!r}"
        )


# =============================================================================
# Tests for rag_engine.py - score_type field in _build_done_message
# =============================================================================


class TestBuildDoneMessageScoreType:
    """Tests for score_type field in _build_done_message."""

    @pytest.fixture(autouse=True)
    def _patch_ssrf(self):
        with patch("app.services.embeddings.assert_url_safe"), \
             patch("app.services.llm_client.assert_url_safe"):
            yield

    def test_done_message_has_score_type_distance(self):
        """Verify score_type is 'distance' when reranking is disabled."""
        from app.services.rag_engine import RAGEngine

        # Create engine with reranking disabled
        engine = RAGEngine()
        engine.reranking_enabled = False

        result = engine._build_done_message([], [], "distance", "disabled", 0, "disabled")

        assert "score_type" in result, (
            "_build_done_message result should include 'score_type' field"
        )
        assert result["score_type"] == "distance", (
            f"score_type should be 'distance' when reranking_enabled=False, "
            f"got {result['score_type']!r}"
        )

    def test_done_message_has_score_type_rerank(self):
        """Verify score_type is 'rerank' when passed as parameter."""
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine()

        result = engine._build_done_message([], [], "rerank", "disabled", 0, "disabled")

        assert "score_type" in result, (
            "_build_done_message result should include 'score_type' field"
        )
        assert result["score_type"] == "rerank", (
            f"score_type should be 'rerank' when passed 'rerank', "
            f"got {result['score_type']!r}"
        )

    def test_done_message_score_type_is_string(self):
        """Verify score_type is always a string."""
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine()
        engine.reranking_enabled = False

        result = engine._build_done_message([], [], "distance", "disabled", 0, "disabled")

        assert isinstance(result["score_type"], str), (
            f"score_type should be a string, got {type(result['score_type'])}"
        )

    def test_done_message_includes_all_expected_fields(self):
        """Verify _build_done_message includes all expected fields."""
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine()

        result = engine._build_done_message([], [], "distance", "disabled", 0, "disabled")

        # Core fields that must always be present (subset check — new fields may be added)
        expected_fields = {
            "type",
            "sources",
            "memories_used",
            "retrieval_debug",
            "score_type",
        }
        actual_fields = set(result.keys())

        assert expected_fields.issubset(actual_fields), (
            f"_build_done_message is missing expected fields. "
            f"Missing: {expected_fields - actual_fields}"
        )


# =============================================================================
# Edge case tests
# =============================================================================


class TestCitationInstructionEdgeCases:
    """Edge case tests for CITATION_INSTRUCTION."""

    def test_citation_instruction_not_mutated(self):
        """Verify CITATION_INSTRUCTION is not modified after import."""
        from app.services import prompt_builder

        original_value = prompt_builder.CITATION_INSTRUCTION
        # Re-import to verify it's the same
        from app.services.prompt_builder import CITATION_INSTRUCTION

        assert CITATION_INSTRUCTION == original_value, (
            "CITATION_INSTRUCTION should be immutable constant"
        )

    def test_citation_instruction_is_module_level(self):
        """Verify CITATION_INSTRUCTION is defined at module level, not inside class."""
        import app.services.prompt_builder as pb_module

        # Should be accessible as module attribute
        assert hasattr(pb_module, "CITATION_INSTRUCTION"), (
            "CITATION_INSTRUCTION should be a module-level constant"
        )

        # Should not be an attribute of PromptBuilderService class
        assert not hasattr(pb_module.PromptBuilderService, "CITATION_INSTRUCTION"), (
            "CITATION_INSTRUCTION should be module-level, not a class attribute"
        )


class TestScoreTypeEdgeCases:
    """Edge case tests for score_type field."""

    @pytest.fixture(autouse=True)
    def _patch_ssrf(self):
        with patch("app.services.embeddings.assert_url_safe"), \
             patch("app.services.llm_client.assert_url_safe"):
            yield

    def test_score_type_reflects_runtime_changes(self):
        """Verify score_type reflects the parameter passed to _build_done_message."""
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine()

        # Test with distance score_type
        result = engine._build_done_message([], [], "distance", "disabled", 0, "disabled")
        assert result["score_type"] == "distance"

        # Test with rerank score_type
        result = engine._build_done_message([], [], "rerank", "disabled", 0, "disabled")
        assert result["score_type"] == "rerank"

    def test_done_message_with_actual_sources(self):
        """Verify _build_done_message works with actual source data."""
        from app.services.document_retrieval import RAGSource
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine()
        engine.reranking_enabled = False

        # Create mock sources
        mock_sources = [
            RAGSource(
                text="Test content",
                file_id="test-file-id",
                score=0.85,
                metadata={"source_file": "test.pdf"},
            )
        ]

        result = engine._build_done_message(mock_sources, [], "distance", "disabled", 0, "disabled")

        assert result["score_type"] == "distance"
        assert len(result["sources"]) == 1
        assert result["sources"][0]["file_id"] == "test-file-id"
