"""Regression tests: citation confidence scoring must read full chunk text.

Bug (chat-output-regression): rag_engine.py's citation confidence scoring
block built ``source_texts`` from ``done_msg["sources"]`` — the source
dicts produced by ``document_retrieval.to_source_metadata`` — which carry
only a 300-char display "snippet", never the full chunk "text". That made
``source_texts`` all-empty strings, so ``score_citations`` scored every
citation against nothing: confidence was always 0.0 and every uncited
sentence was flagged unverifiable.

The fix reads ``source_texts`` from ``relevant_chunks`` (the retrieved
``RAGSource`` objects that still carry full ``.text``) instead, relying on
the index alignment ``relevant_chunks[i] <-> done_msg["sources"][i] <-> S{i+1}``
that ``_build_done_message`` guarantees via a straight, unfiltered
``enumerate(relevant_chunks)``.

These tests drive the real ``RAGEngine.query()`` end to end (same harness
pattern as test_rag_engine_score_tracking.py) so the assertions exercise the
actual wiring, not a mocked seam.
"""

import os
import sys
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors test_rag_engine_score_tracking.py)
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
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from app.config import settings
from app.services.rag_engine import RAGEngine


class FakeEmbeddingService:
    """Deterministic fake embedding service for testing."""

    def __init__(self, embedding: Optional[List[float]] = None):
        self.embedding = embedding if embedding is not None else [0.1, 0.2, 0.3]

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding.copy()

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embedding.copy() for _ in texts]

    async def embed_query_sparse(self, text: str) -> Optional[Dict[str, Any]]:
        return None


class FakeVectorStore:
    """Deterministic fake vector store for testing."""

    def __init__(self, results: Optional[List[Dict[str, Any]]] = None):
        self._results = results if results is not None else []
        self._fts_exceptions = 0

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        filter_expr: Optional[str] = None,
        vault_id: Optional[str] = None,
        query_text: Optional[str] = None,
        hybrid: bool = False,
        hybrid_alpha: float = 0.5,
        query_sparse: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        return self._results[:limit]

    async def get_chunks_by_uid(self, chunk_uids: List[str]) -> List[Dict[str, Any]]:
        return []

    def get_fts_exceptions(self) -> int:
        count = self._fts_exceptions
        self._fts_exceptions = 0
        return count


class FakeMemoryStore:
    """Deterministic fake memory store for testing."""

    def detect_memory_intent(self, text: str) -> Optional[str]:
        return None

    def search_memories(self, query: str, limit: int, vault_id=None, include_global: bool = False) -> list:
        return []


class FakeLLMClient:
    """Deterministic fake LLM client that always returns a fixed response."""

    def __init__(self, response: str = ""):
        self._response = response
        self.last_messages: Optional[List[Dict[str, str]]] = None

    async def chat_completion(self, messages: List[Dict[str, str]], **kwargs) -> str:
        self.last_messages = messages
        return self._response

    async def chat_completion_stream(
        self, messages: List[Dict[str, str]], **kwargs
    ) -> AsyncIterator[str]:
        self.last_messages = messages
        yield self._response


@pytest.mark.asyncio
class TestCitationConfidenceUsesChunkText:
    """Regression coverage for the source_texts-from-relevant_chunks fix."""

    def _create_engine(self, llm_response: str, initial_results: List[Dict[str, Any]]):
        """Create a RAGEngine wired with fakes and deterministic retrieval."""
        fake_embedding = FakeEmbeddingService()
        fake_vector = FakeVectorStore(results=initial_results)
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response=llm_response)

        engine = RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm,
            reranking_service=None,
        )
        engine.reranking_enabled = False
        engine.hybrid_search_enabled = False
        return engine

    async def _run_query(self, engine: RAGEngine) -> Dict[str, Any]:
        messages = []
        async for msg in engine.query("test query", []):
            messages.append(msg)
        done_msg = next((m for m in messages if m.get("type") == "done"), None)
        assert done_msg is not None, "No done message found"
        return done_msg

    async def test_citation_confidence_uses_chunk_text_not_snippet(self, monkeypatch):
        """Reproduces the bug shape: sources dicts carry no 'text', only a
        snippet, while the RAGSource chunks carry the full text. Citation
        scoring must key off the chunk text (nonzero confidence) rather than
        the missing dict field (which would silently give confidence 0.0 for
        every citation, as it did before the fix).
        """
        # Context distillation embeds sentences with the fake (constant)
        # embedding, which would make every sentence look like a duplicate
        # of every other and collapse the crafted chunk texts. Disable it so
        # this test isolates the citation-confidence scoring path.
        monkeypatch.setattr(settings, "context_distillation_enabled", False)

        chunk0_text = (
            "Photosynthesis converts sunlight into chemical energy stored "
            "inside plant leaves as glucose molecules for later use."
        )
        chunk1_text = (
            "Central bank interest rate cuts caused the stock market index "
            "to rally sharply within a single trading session."
        )
        initial_results = [
            {"id": "chunk1", "text": chunk0_text, "file_id": "doc1", "_distance": 0.1, "metadata": {}},
            {"id": "chunk2", "text": chunk1_text, "file_id": "doc2", "_distance": 0.2, "metadata": {}},
        ]

        # Cite each source verbatim so a correct fix scores high overlap
        # regardless of whether the metric is Jaccard or containment.
        llm_response = (
            "Photosynthesis converts sunlight into chemical energy stored inside "
            "plant leaves as glucose molecules for later use [S1]. "
            "Central bank interest rate cuts caused the stock market index to "
            "rally sharply within a single trading session [S2]. "
            "Bananas are a great source of potassium for daily nutrition."
        )

        engine = self._create_engine(llm_response, initial_results)
        done_msg = await self._run_query(engine)

        sources = done_msg.get("sources", [])
        assert len(sources) == 2, f"Expected 2 sources, got {len(sources)}: {sources}"

        # Confirm the trap is real: source dicts carry only a display
        # snippet, never the full chunk text.
        for source in sources:
            assert "text" not in source, (
                f"source dict unexpectedly has a 'text' key: {source}"
            )
            assert "snippet" in source

        confidence = done_msg.get("citation_confidence", {})
        # Under the old bug, source_texts was ["", ""] and both of these
        # would be exactly 0.0. A working fix scores near-verbatim citations
        # with clearly nonzero confidence under either the Jaccard or the
        # containment overlap metric.
        assert confidence.get("S1", 0.0) > 0.2, (
            f"Expected nonzero S1 confidence, got {confidence}"
        )
        assert confidence.get("S2", 0.0) > 0.2, (
            f"Expected nonzero S2 confidence, got {confidence}"
        )

        # The unrelated, uncited sentence should still be flagged
        # unverifiable — proving source_texts reflect real, distinguishing
        # content rather than e.g. all sources matching everything.
        unverifiable = done_msg.get("unverifiable_claims", [])
        assert any("Bananas" in c for c in unverifiable), (
            f"Expected the unrelated sentence to be flagged unverifiable, got {unverifiable}"
        )

    async def test_citation_confidence_label_alignment_s2_maps_to_second_chunk(self, monkeypatch):
        """S2 confidence must reflect relevant_chunks[1].text specifically —
        not relevant_chunks[0] or relevant_chunks[2] — proving the index
        alignment relevant_chunks[i] <-> sources[i] <-> S{i+1} holds.
        """
        monkeypatch.setattr(settings, "context_distillation_enabled", False)

        # Each chunk carries a unique "fingerprint" word absent from the
        # others, so a misaligned source_texts list would score near zero.
        chunk0_text = "Aardvarks are nocturnal mammals native to sub-Saharan Africa that dig extensive burrows."
        chunk1_text = "Bumblebee colonies communicate through vibration patterns known as buzz pollination signals."
        chunk2_text = "Cacti store water in thick fleshy stems to survive long droughts in arid desert climates."

        initial_results = [
            {"id": "chunk1", "text": chunk0_text, "file_id": "doc1", "_distance": 0.1, "metadata": {}},
            {"id": "chunk2", "text": chunk1_text, "file_id": "doc2", "_distance": 0.15, "metadata": {}},
            {"id": "chunk3", "text": chunk2_text, "file_id": "doc3", "_distance": 0.2, "metadata": {}},
        ]

        llm_response = (
            "Bumblebee colonies communicate through vibration patterns known as "
            "buzz pollination signals [S2]. This claim is cited to the second source."
        )

        engine = self._create_engine(llm_response, initial_results)
        done_msg = await self._run_query(engine)

        sources = done_msg.get("sources", [])
        assert len(sources) == 3, f"Expected 3 sources, got {len(sources)}: {sources}"
        assert sources[1]["source_label"] == "S2"

        confidence = done_msg.get("citation_confidence", {})
        # If source_texts were misaligned (e.g. still index-0 chunk, or
        # empty snippets), this would score at or near 0.0 since chunk0 and
        # chunk2 share no vocabulary with the cited bumblebee sentence.
        assert confidence.get("S2", 0.0) > 0.2, (
            f"Expected S2 confidence to reflect relevant_chunks[1] (bumblebee text), got {confidence}"
        )
