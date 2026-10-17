"""Per-sentence citation support scoring across label families (issue #714).

AC1-AC3: the done payload's top-level ``citation_confidence`` must carry a
numeric per-sentence overlap score for every citation family the answer
cites — memory [M#], wiki [W#], and KMS [K#] — not just document [S#].
Each test below drives a non-streaming ``RAGEngine.query()`` with the fake
harness from test_rag_engine.py and a real WikiEvidence/KMSEvidence record
injected through the engine's retrieval adapters.

RED at base, GREEN after the fix: at base ``score_citations`` only scores
[S#] labels, so ``citation_confidence`` has no M/W/K entries at all.
"""

import os
import sys
import types
from typing import Any, Dict, List, Optional, cast
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
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

from app.config import settings
from app.services.embeddings import EmbeddingService
from app.services.kms_retrieval import KMSEvidence
from app.services.llm_client import LLMClient
from app.services.memory_store import MemoryRecord, MemoryStore
from app.services.rag_engine import RAGEngine
from app.services.vector_store import VectorStore
from app.services.wiki_retrieval import WikiEvidence


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests."""
    with patch('app.services.embeddings.assert_url_safe'), \
         patch('app.services.llm_client.assert_url_safe'):
        yield


class FakeEmbeddingService:
    def __init__(self, embedding: List[float]):
        self.embedding = embedding

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding

    async def embed_passage(self, text: str) -> List[float]:
        return self.embedding


class FakeVectorStore:
    def __init__(self, results: List[Dict]):
        self._results = results

    async def search(self, embedding: List[float], limit: int = 10, filter_expr=None, vault_id=None, query_text=None, hybrid=False, **kwargs):
        results = self._results
        if vault_id is not None:
            target = str(vault_id)
            results = [r for r in results if str(r.get("vault_id")) == target]
        return results[:limit]

    def get_chunks_by_uid(self, chunk_uids: List[str]):
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class FakeMemoryStore:
    def __init__(self, intent: Optional[str] = None, memories: Optional[List[MemoryRecord]] = None):
        self.intent = intent
        self._memories = memories or []
        self.added: List[str] = []

    def detect_memory_intent(self, text: str):
        return self.intent

    def find_memory_by_content(self, content: str, source, vault_id):
        return None

    def add_memory(self, content: str, category=None, tags=None, source=None, vault_id=None):
        self.added.append(content)
        return MemoryRecord(id=1, content=content, category=category, tags=tags, source=source, created_at=None, updated_at=None)

    def search_memories(self, query: str, limit: int = 5, vault_id=None, include_global: bool = False):
        return self._memories[:limit]


class FakeLLMClient:
    def __init__(self, response: str, stream_chunks: Optional[List[str]] = None):
        self._response = response
        self._stream_chunks = stream_chunks or []

    async def chat_completion(self, messages, **kwargs):
        return self._response

    async def chat_completion_stream(self, messages, **kwargs):
        for chunk in self._stream_chunks:
            yield chunk


class FakeWikiRetrieval:
    """Sync retrieve() is fine — the engine wraps it in asyncio.to_thread."""

    def __init__(self, evidence: List[WikiEvidence]):
        self._evidence = evidence

    def retrieve(self, query: str, vault_id) -> List[WikiEvidence]:
        return list(self._evidence)


class FakeKmsRetrieval:
    """Sync retrieve() is fine — the engine wraps it in asyncio.to_thread."""

    def __init__(self, evidence: List[KMSEvidence]):
        self._evidence = evidence

    def retrieve(self, query: str, vault_id) -> List[KMSEvidence]:
        return list(self._evidence)


def _build_engine(
    memory: Optional[MemoryRecord],
    response: str,
    vector_results: Optional[List[Dict]] = None,
    wiki_evidence: Optional[List[WikiEvidence]] = None,
    kms_evidence: Optional[List[KMSEvidence]] = None,
) -> RAGEngine:
    engine = RAGEngine()
    engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
    engine.vector_store = cast(VectorStore, FakeVectorStore(vector_results or []))
    engine.memory_store = cast(
        MemoryStore, FakeMemoryStore(memories=[memory] if memory else [])
    )
    engine.llm_client = cast(LLMClient, FakeLLMClient(response=response))
    if wiki_evidence:
        engine._wiki_retrieval = FakeWikiRetrieval(wiki_evidence)  # type: ignore[assignment]
    if kms_evidence:
        engine._kms_retrieval = FakeKmsRetrieval(kms_evidence)  # type: ignore[assignment]
    return engine


async def _run_done(engine: RAGEngine) -> Dict[str, Any]:
    with patch.object(settings, "query_transformation_enabled", False), \
         patch.object(settings, "kms_enabled", True):
        results = [
            msg
            async for msg in engine.query("query", [], stream=False, vault_id=1)
        ]
    done = [r for r in results if r.get("type") == "done"][0]
    return done


async def test_memory_citation_gets_support_score():
    """AC1 — RED at base, GREEN after the fix.

    A [M1] citation whose memory text shares tokens with the citing
    sentence must yield a numeric support score in citation_confidence.
    """
    memory = MemoryRecord(
        id=1,
        content="The capital of France is Paris",
        category=None,
        tags=None,
        source="test",
        created_at=None,
        updated_at=None,
    )
    vector_results = [
        {"text": "France is a country in Europe.", "file_id": "file1", "metadata": {"source_file": "doc.md"}, "score": 0.9},
    ]
    engine = _build_engine(
        memory=memory,
        # Label inside the sentence, before the period.
        response="The capital of France is Paris [M1].",
        vector_results=vector_results,
    )
    done = await _run_done(engine)

    # Precondition: the harness delivered the memory and it was cited.
    assert [m["memory_label"] for m in done["memories_used"]] == ["M1"]
    conf = done["citation_confidence"]
    assert conf.get("M1") is not None


async def test_zero_overlap_wiki_citation_scores_zero():
    """AC2 — RED at base, GREEN after the fix.

    A [W1] citation sharing NO tokens with the wiki evidence text must
    score exactly 0.0.
    """
    wiki = WikiEvidence(
        label_placeholder="W1",
        page_id=1,
        claim_id=1,
        title="Entanglement basics",
        slug="entanglement-basics",
        page_type="note",
        # Token-disjoint from the citing sentence "Zebras roam the savanna."
        claim_text="Quantum entanglement couples particle states.",
        excerpt="Quantum entanglement couples particle states.",
        confidence=0.9,
        page_status="completed",
        claim_status=None,
        score=1.0,
        score_type="page_fts",
        freshness=None,
        source_count=1,
        provenance_summary="p",
    )
    engine = _build_engine(
        memory=None,
        response="Zebras roam the savanna [W1].",
        vector_results=[],
        wiki_evidence=[wiki],
    )
    done = await _run_done(engine)

    # Precondition: the harness delivered the wiki evidence and it was cited.
    assert [w["wiki_label"] for w in done["wiki_used"]] == ["W1"]
    conf = done["citation_confidence"]
    assert conf.get("W1") == 0.0


async def test_kms_citation_gets_support_score():
    """AC3 — RED at base, GREEN after the fix.

    A [K1] citation whose KMS evidence shares tokens with the citing
    sentence must score above zero.
    """
    kms = KMSEvidence(
        label_placeholder="K1",
        entry_id=1,
        slug="pythagorean-theorem",
        title="Pythagorean theorem",
        summary="The pythagorean theorem relates the sides of a right triangle.",
        excerpt="The pythagorean theorem relates the sides of a right triangle.",
        tags=[],
        status="active",
        source_type="doc",
        file_id=None,
        score=1.0,
        score_type="kms_fts",
    )
    engine = _build_engine(
        memory=None,
        response="The pythagorean theorem relates triangle sides [K1].",
        vector_results=[],
        kms_evidence=[kms],
    )
    done = await _run_done(engine)

    # Precondition: the harness delivered the KMS evidence and it was cited.
    assert [k["kms_label"] for k in done["kms_used"]] == ["K1"]
    score = done["citation_confidence"].get("K1")
    assert score is not None
    assert score > 0
