"""Engine-seam value pins for issue #714 support scoring (PR #966 feedback round).

The frozen AC tests assert presence (M1 is not None) and exact zero (W1 == 0.0),
which cannot distinguish a wired evidence body from an empty-string body — a
regression that scored every memory/wiki citation 0.0 in production would keep
them green. These tests pin the ENGINE-side body extraction (rag_engine's
support_by_label construction) at the seam that builds it: a real
RAGEngine.query() done payload must carry a strictly positive score when the
evidence body shares tokens with the citing sentence, and a cited phantom label
must never gain a confidence key.

RED at base would be a wiring regression (not applicable — these run against
the wired engine); they kill the `support_by_label[label] = ""` mutation class.
"""

import os
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _name in ("lancedb", "pyarrow"):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = types.ModuleType(_name)
try:
    from unstructured.partition.auto import partition  # noqa: F401
except Exception:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *a, **k: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *a, **k: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.kms_retrieval import KMSEvidence  # noqa: E402
from app.services.memory_store import MemoryRecord  # noqa: E402
from app.services.rag_engine import RAGEngine  # noqa: E402
from app.services.wiki_retrieval import WikiEvidence  # noqa: E402


class FakeEmbeddingService:
    def __init__(self, embedding):
        self.embedding = embedding

    async def embed_single(self, text):
        return self.embedding

    async def embed_passage(self, text):
        return self.embedding


class FakeVectorStore:
    def __init__(self, results):
        self._results = results

    async def search(self, embedding, limit=10, filter_expr=None, vault_id=None,
                     query_text=None, hybrid=False, **kwargs):
        results = self._results
        if vault_id is not None:
            target = str(vault_id)
            results = [r for r in results if str(r.get("vault_id")) == target]
        return results[:limit]

    def get_chunks_by_uid(self, chunk_uids):
        return []

    def get_fts_exceptions(self):
        return 0


class FakeMemoryStore:
    def __init__(self, memories=None):
        self._memories = memories or []

    def detect_memory_intent(self, text):
        return None

    def find_memory_by_content(self, content, source, vault_id):
        return None

    def add_memory(self, content, category=None, tags=None, source=None, vault_id=None):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return self._memories[:limit]


class FakeLLMClient:
    def __init__(self, response):
        self._response = response

    async def chat_completion(self, messages, **kwargs):
        return self._response

    async def chat_completion_stream(self, messages, **kwargs):
        yield self._response


class FakeWikiRetrieval:
    def __init__(self, items):
        self._items = items

    def retrieve(self, query, vault_id):
        return self._items


class FakeKmsRetrieval:
    def __init__(self, items):
        self._items = items

    def retrieve(self, query, vault_id):
        return self._items


def _wiki_item(claim_text):
    return WikiEvidence(
        label_placeholder="W1",
        page_id=1,
        claim_id=1,
        title="Savanna fauna",
        slug="savanna-fauna",
        page_type="concept",
        claim_text=claim_text,
        excerpt="excerpt body",
        confidence=0.9,
        page_status="verified",
        claim_status="active",
        score=1.0,
        score_type="page_fts",
        freshness=None,
        source_count=1,
        provenance_summary="p",
    )


def _kms_item(excerpt, summary):
    return KMSEvidence(
        label_placeholder="K1",
        entry_id=7,
        slug="pythagorean-theorem",
        title="Pythagorean theorem",
        summary=summary,
        excerpt=excerpt,
        tags=[],
        status="active",
        source_type="math",
        file_id=None,
        score=1.0,
        score_type="kms_fts",
    )


def _build_engine(memories=None, wiki_items=None, kms_items=None):
    engine = RAGEngine()
    engine.embedding_service = FakeEmbeddingService([0.1, 0.2])
    engine.vector_store = FakeVectorStore(
        [{"text": "France is a country in Europe.", "file_id": "file1",
          "metadata": {"source_file": "doc.md"}, "score": 0.9}]
    )
    engine.memory_store = FakeMemoryStore(memories=memories or [])
    if wiki_items is not None:
        engine._wiki_retrieval = FakeWikiRetrieval(wiki_items)
    if kms_items is not None:
        engine._kms_retrieval = FakeKmsRetrieval(kms_items)
    engine.llm_client = FakeLLMClient(response="")
    return engine


async def _done_for(engine, question, answer):
    engine.llm_client = FakeLLMClient(response=answer)
    results = [m async for m in engine.query(question, [], stream=False, vault_id=1)]
    done = [r for r in results if r.get("type") == "done"]
    assert done, "engine produced no done chunk"
    return done[0]


@pytest.fixture()
def _ssrf_and_settings():
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ), patch.object(settings, "query_transformation_enabled", False), patch.object(
        settings, "kms_enabled", True
    ):
        yield


@pytest.mark.asyncio
async def test_memory_body_wiring_value_pinned(_ssrf_and_settings):
    """The memory CONTENT must reach the scorer: a shared-token body scores > 0.

    Kills the regression where rag_engine wires support_by_label[label] to an
    empty string (that mutant kept the frozen presence-assert green).
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
    engine = _build_engine(memories=[memory])
    done = await _done_for(engine, "What is the capital of France?",
                           "The capital of France is Paris [M1].")
    assert [m.get("memory_label") for m in done["memories_used"]] == ["M1"]
    assert done["citation_confidence"].get("M1", 0.0) > 0.0


@pytest.mark.asyncio
async def test_wiki_body_wiring_value_pinned(_ssrf_and_settings):
    """The wiki CLAIM_TEXT must reach the scorer: a shared-token body scores > 0.

    Kills the same mutant class on the wiki branch (the frozen AC2 zero-overlap
    assert cannot distinguish an absent body from a disjoint one).
    """
    engine = _build_engine(wiki_items=[_wiki_item("Zebras roam the savanna in herds")])
    done = await _done_for(engine, "What roams the savanna?",
                           "Zebras roam the savanna [W1].")
    assert done["citation_confidence"].get("W1", 0.0) > 0.0


@pytest.mark.asyncio
async def test_cited_phantom_label_never_scores(_ssrf_and_settings):
    """A cited label that was never offered gets no confidence key.

    The engine builds the registry from offered-and-cited evidence only
    (_build_done_message intersects candidates with cited labels), so an
    answer citing [M9] with no ninth memory must not produce an M9 key.
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
    engine = _build_engine(memories=[memory])
    done = await _done_for(engine, "Tell me about widgets.",
                           "Widgets are blue [M9].")
    assert "M9" not in done["citation_confidence"]
    assert [m.get("memory_label") for m in done["memories_used"]] == []
