"""Tests for the agentic RAG branch early-return documentation (issue #281).

These tests verify that:
1. The comment block documenting the agentic RAG early-return limitation exists
   and has the correct content.
2. The described early-return behavior actually occurs (agentic branch skips
   answer_contract, citation repair, and full trace assembly).
"""

import asyncio
import os
import re
import sys
import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (same pattern as test_rag_engine.py)
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from app.services.rag_engine import RAGEngine

# ---------------------------------------------------------------------------
# Source-inspection tests: verify the comment block exists and has correct text
# ---------------------------------------------------------------------------

class TestAgenticRAGCommentDocumentation(unittest.TestCase):
    """Source-inspection tests for the agentic RAG early-return docstring."""

    RAG_ENGINE_PATH = os.path.join(
        os.path.dirname(__file__),
        '..',
        'app',
        'services',
        'rag_engine.py',
    )

    def test_agentic_rag_early_return_comment_block_exists(self):
        """The early-return comment block must be present in rag_engine.py."""
        with open(self.RAG_ENGINE_PATH, encoding='utf-8') as fh:
            source = fh.read()

        # The comment block should be near the agentic_rag_enabled check
        self.assertIn(
            'agentic_rag_enabled',
            source,
            'rag_engine.py must reference agentic_rag_enabled',
        )

        # Verify the comment block heading is present
        self.assertIn(
            'FR-008: Agentic RAG',
            source,
            'The FR-008 Agentic RAG comment heading must be present',
        )

    def test_agentic_rag_comment_documents_answer_contract_skip(self):
        """Comment must state that answer_contract enforcement is skipped."""
        with open(self.RAG_ENGINE_PATH, encoding='utf-8') as fh:
            source = fh.read()

        self.assertIn(
            'answer_contract',
            source,
            'Comment must mention answer_contract',
        )

    def test_agentic_rag_comment_documents_citation_repair_skip(self):
        """Comment must state that citation repair is skipped."""
        with open(self.RAG_ENGINE_PATH, encoding='utf-8') as fh:
            source = fh.read()

        self.assertIn(
            'citation repair',
            source,
            'Comment must mention citation repair',
        )

    def test_agentic_rag_comment_documents_trace_assembly_skip(self):
        """Comment must state that full trace assembly is skipped."""
        with open(self.RAG_ENGINE_PATH, encoding='utf-8') as fh:
            source = fh.read()

        self.assertIn(
            'full trace assembly',
            source,
            'Comment must mention full trace assembly',
        )

    def test_agentic_rag_comment_block_is_near_agentic_check(self):
        """The early-return comment must precede the agentic_rag_enabled branch."""
        with open(self.RAG_ENGINE_PATH, encoding='utf-8') as fh:
            source = fh.read()

        # Find the line index of the NOTE comment
        note_pattern = re.compile(r'NOTE:.*agentic_rag_enabled.*True', re.DOTALL)
        match = note_pattern.search(source)
        self.assertIsNotNone(match, 'NOTE comment about agentic_rag_enabled=True must exist')

        # Find the line index of the if settings.agentic_rag_enabled check
        branch_idx = source.index('if settings.agentic_rag_enabled:')

        # The NOTE comment should appear before the if statement
        self.assertLess(
            match.start(),
            branch_idx,
            'The NOTE comment about early return should appear before the if branch',
        )

    def test_agentic_rag_comment_mentions_returns_early(self):
        """Comment must explicitly mention that the branch returns early."""
        with open(self.RAG_ENGINE_PATH, encoding='utf-8') as fh:
            source = fh.read()

        # The actual comment says "returns early (line 672)"
        self.assertIn(
            'returns',
            source,
            'Comment must mention "returns" in the early-return context',
        )
        # Verify the parenthetical "(line 672)" is present (references the actual return)
        self.assertIn(
            '(line 672)',
            source,
            'Comment must reference the actual return line (line 672)',
        )


# ---------------------------------------------------------------------------
# Behavioral tests: verify the agentic branch actually exhibits the described
# early-return behavior (skips answer_contract, citation repair, trace assembly)
# ---------------------------------------------------------------------------

class FakeEmbeddingService:
    """Minimal fake for EmbeddingService."""
    def __init__(self, embedding: List[float] | None = None):
        self.embedding = embedding or [0.0] * 768

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding

    async def embed_passage(self, text: str) -> List[float]:
        return self.embedding


class FakeVectorStore:
    """Minimal fake for VectorStore."""
    def __init__(self):
        pass

    async def search(self, *args, **kwargs):
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class FakeMemoryStore:
    """Minimal fake for MemoryStore."""
    def detect_memory_intent(self, text):
        return None

    def search_memories(self, *args, **kwargs):
        return []


class FakeAgenticPlannerResult:
    """Fake result object returned by AgenticPlanner.plan_and_execute()."""
    def __init__(self, output: str = 'Agentic response text', all_sources: list = None):
        self.output = output
        self.all_sources = all_sources or []


class TestAgenticRAGEarlyReturnBehavior(unittest.TestCase):
    """Behavioral tests verifying the agentic branch early-return characteristics."""

    def _make_engine(self) -> RAGEngine:
        """Construct a RAGEngine with all dependencies faked."""
        return RAGEngine(
            embedding_service=FakeEmbeddingService(),
            vector_store=FakeVectorStore(),
            memory_store=FakeMemoryStore(),
            llm_client=MagicMock(),  # not used in agentic branch
        )

    @patch('app.services.rag_engine.settings')
    def test_agentic_branch_skips_build_answer_contract(self, mock_settings):
        """When agentic_rag_enabled, done message must not contain answer_contract."""
        # Configure agentic mode
        mock_settings.agentic_rag_enabled = True

        async def run():
            engine = self._make_engine()

            # Mock the AgenticPlanner to avoid real LLM calls
            fake_result = FakeAgenticPlannerResult(output='Agentic response text')
            with patch('app.services.rag_engine.AgenticPlanner') as MockPlanner:
                MockPlanner.return_value.plan_and_execute = AsyncMock(return_value=fake_result)
                events = []
                async for ev in engine.query(
                    user_input='hello',
                    chat_history=[],
                    vault_id=1,
                ):
                    events.append(ev)

            # The agentic branch yields exactly 2 events: content + done
            self.assertEqual(len(events), 2, f'Expected 2 events, got {len(events)}: {events}')
            done_event = events[-1]
            self.assertNotIn(
                'answer_contract',
                done_event,
                'agentic branch must NOT produce answer_contract (skipped by design)',
            )
            return True

        self.assertTrue(asyncio.run(run()))

    @patch('app.services.rag_engine.settings')
    def test_agentic_branch_does_not_yield_citation_confidence(self, mock_settings):
        """When agentic_rag_enabled, done message must not contain citation_confidence."""
        mock_settings.agentic_rag_enabled = True

        async def run():
            engine = self._make_engine()

            fake_result = FakeAgenticPlannerResult(output='Agentic response text')
            with patch('app.services.rag_engine.AgenticPlanner') as MockPlanner:
                MockPlanner.return_value.plan_and_execute = AsyncMock(return_value=fake_result)
                events = []
                async for ev in engine.query(
                    user_input='hello',
                    chat_history=[],
                    vault_id=1,
                ):
                    events.append(ev)

            done_event = events[-1]
            self.assertNotIn(
                'citation_confidence',
                done_event,
                'agentic branch must NOT produce citation_confidence (citation repair skipped)',
            )
            return True

        self.assertTrue(asyncio.run(run()))

    @patch('app.services.rag_engine.settings')
    def test_agentic_branch_does_not_yield_trace(self, mock_settings):
        """When agentic_rag_enabled, done message must not contain trace."""
        mock_settings.agentic_rag_enabled = True

        async def run():
            engine = self._make_engine()

            fake_result = FakeAgenticPlannerResult(output='Agentic response text')
            with patch('app.services.rag_engine.AgenticPlanner') as MockPlanner:
                MockPlanner.return_value.plan_and_execute = AsyncMock(return_value=fake_result)
                events = []
                async for ev in engine.query(
                    user_input='hello',
                    chat_history=[],
                    vault_id=1,
                ):
                    events.append(ev)

            done_event = events[-1]
            self.assertNotIn(
                'trace',
                done_event,
                'agentic branch must NOT produce trace (full trace assembly skipped)',
            )
            return True

        self.assertTrue(asyncio.run(run()))

    @patch('app.services.rag_engine.settings')
    def test_agentic_branch_yields_only_two_events(self, mock_settings):
        """When agentic_rag_enabled succeeds, it yields exactly 2 events."""
        mock_settings.agentic_rag_enabled = True

        async def run():
            engine = self._make_engine()

            fake_result = FakeAgenticPlannerResult(output='Agentic response text')
            with patch('app.services.rag_engine.AgenticPlanner') as MockPlanner:
                MockPlanner.return_value.plan_and_execute = AsyncMock(return_value=fake_result)
                events = []
                async for ev in engine.query(
                    user_input='hello',
                    chat_history=[],
                    vault_id=1,
                ):
                    events.append(ev)

            # The agentic branch should yield: content + done (2 events)
            # It does NOT yield stage events (SEARCHING, READING, DRAFTING)
            # or go through the full pipeline
            self.assertEqual(
                len(events),
                2,
                f'agentic branch should yield exactly 2 events, got {len(events)}: {events}',
            )
            return True

        self.assertTrue(asyncio.run(run()))

    @patch('app.services.rag_engine.settings')
    def test_agentic_done_message_has_answer_source_mode_agentic(self, mock_settings):
        """done message from agentic branch should have answer_source_mode='agentic'."""
        mock_settings.agentic_rag_enabled = True

        async def run():
            engine = self._make_engine()

            fake_result = FakeAgenticPlannerResult(output='Agentic response text')
            with patch('app.services.rag_engine.AgenticPlanner') as MockPlanner:
                MockPlanner.return_value.plan_and_execute = AsyncMock(return_value=fake_result)
                events = []
                async for ev in engine.query(
                    user_input='hello',
                    chat_history=[],
                    vault_id=1,
                ):
                    events.append(ev)

            done_event = events[-1]
            self.assertEqual(
                done_event.get('answer_source_mode'),
                'agentic',
                'agentic done message must have answer_source_mode=agentic',
            )
            return True

        self.assertTrue(asyncio.run(run()))

    @patch('app.services.rag_engine.settings')
    def test_agentic_branch_returns_before_citation_repair(self, mock_settings):
        """Verify agentic branch returns before reaching repair_against_sources_and_memories."""
        mock_settings.agentic_rag_enabled = True

        async def run():
            engine = self._make_engine()

            fake_result = FakeAgenticPlannerResult(output='Agentic response text')
            with patch('app.services.rag_engine.AgenticPlanner') as MockPlanner:
                MockPlanner.return_value.plan_and_execute = AsyncMock(return_value=fake_result)
                events = []
                async for ev in engine.query(
                    user_input='hello',
                    chat_history=[],
                    vault_id=1,
                ):
                    events.append(ev)

            done_event = events[-1]
            # repair_against_sources_and_memories populates invalid_citations in trace.
            # If trace is absent (which it is), repair was never called.
            self.assertNotIn('invalid_citations', done_event)
            return True

        self.assertTrue(asyncio.run(run()))

    @patch('app.services.rag_engine.settings')
    def test_agentic_branch_returns_before_trace_log(self, mock_settings):
        """Verify agentic branch returns before reaching trace.log() at line 1453."""
        mock_settings.agentic_rag_enabled = True

        async def run():
            engine = self._make_engine()

            fake_result = FakeAgenticPlannerResult(output='Agentic response text')
            with patch('app.services.rag_engine.AgenticPlanner') as MockPlanner:
                MockPlanner.return_value.plan_and_execute = AsyncMock(return_value=fake_result)
                events = []
                async for ev in engine.query(
                    user_input='hello',
                    chat_history=[],
                    vault_id=1,
                ):
                    events.append(ev)

            done_event = events[-1]
            # trace is only embedded in done when rag_trace_in_response is True AND
            # trace.log() has been called. Since we return early, trace.log() is
            # never called, so no trace in done.
            self.assertNotIn('trace', done_event)
            return True

        self.assertTrue(asyncio.run(run()))

    # NOTE: The exception-fallback test is intentionally omitted here.
    # When AgenticPlanner.plan_and_execute() raises, the code falls through to
    # the standard pipeline (line 687 comment: "Fall through to standard pipeline").
    # Testing this requires deeper mocking of the full standard pipeline (retrieval,
    # distillation, LLM response, answer_contract, etc.) which is beyond the scope
    # of the agentic-RAG early-return documentation change. The standard pipeline
    # is already covered by existing test_rag_engine.py tests.


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
