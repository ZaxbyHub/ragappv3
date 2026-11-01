"""Acceptance check for issue #716 — support scoring must tokenize the escaped form (C13).

Seam pinned (absorbed from PR #966 review): ``rag_engine.query()``'s support-text
registry construction (``support_by_label``, rag_engine.py ~L2458-2474) currently
feeds RAW bodies — memory ``content``, wiki ``claim_text or excerpt``, KMS
``excerpt or summary`` — into ``score_citations``, while the prompt the model
actually saw rendered those bodies XML-escaped via ``prompt_builder._xml_escape``
(``html.escape`` with quote=True): the ``<memory>`` block at ~L365, wiki bodies at
~L157, KMS bodies at ~L174. A body containing apostrophes / ``&`` is rendered as
``&#x27;`` / ``&amp;`` fragments, so the model echoes escaped fragments while the
engine tokenizes the raw body — different token sets, wrong support score.

RED at base (665bbd09): the engine's registry holds the raw body, so the done
payload's [M1] confidence equals the RAW-body overlap (0.25 for the pair below),
not the escaped-parity score the acceptance check demands (5/6).

Harness reused from tests/test_c09_support_scoring.py (#966): fake
embedding/vector/memory/LLM adapters around a real ``RAGEngine.query()``.
"""

import os
import sys
import types
from html import escape, unescape
from typing import Dict, List, Optional, cast
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same preamble as test_c09_support_scoring).
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition  # noqa: F401
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
from app.services.citation_validator import score_citations
from app.services.embeddings import EmbeddingService
from app.services.llm_client import LLMClient
from app.services.memory_store import MemoryRecord, MemoryStore
from app.services.rag_engine import RAGEngine
from app.services.vector_store import VectorStore

#: Memory body with characters the prompt renderer XML-escapes.
MEMORY_BODY = "O'Brien & Co's revenue grew"

#: What the model saw in the prompt (prompt_builder._xml_escape) and therefore
#: echoes back: the XML-escaped fragments, plus its [M1] citation.
MODEL_ANSWER = "O&#x27;Brien &amp; Co&#x27;s revenue grew [M1]."


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


def _build_engine(memory: Optional[MemoryRecord], response: str, vector_results: Optional[List[Dict]] = None) -> RAGEngine:
    engine = RAGEngine()
    engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
    engine.vector_store = cast(VectorStore, FakeVectorStore(vector_results or []))
    engine.memory_store = cast(
        MemoryStore, FakeMemoryStore(memories=[memory] if memory else [])
    )
    engine.llm_client = cast(LLMClient, FakeLLMClient(response=response))
    return engine


async def _run_done(engine: RAGEngine) -> Dict:
    with patch.object(settings, "query_transformation_enabled", False), \
         patch.object(settings, "kms_enabled", True):
        results = [
            msg
            async for msg in engine.query("query", [], stream=False, vault_id=1)
        ]
    done = [r for r in results if r.get("type") == "done"][0]
    return done


async def test_support_scoring_is_premise_invariant():
    """C13 — the engine's [M1] support score must be PREMISE-INVARIANT.

    AMENDED (CHECK_WRONG, #716 feedback PRR-003): the original check pinned
    escaped-evidence scoring on the assumption that the model echoes the
    escaped prompt fragments. Review probes showed real models emit natural
    characters, so escaping the evidence INVERTED the score (0.875 → 0.4545
    on this pair). The fix keeps registry bodies raw and unescapes the model
    side (wiki_curator's keep-source-raw pattern), so the score equals the
    raw-form overlap under BOTH answer shapes.

    Drives the real ``RAGEngine.query()`` (harness from test_c09_support_scoring)
    with a memory whose body contains apostrophes and ``&``, twice: once with
    an answer echoing the XML-escaped fragments, once with the natural-
    character form. Each done payload's ``citation_confidence["M1"]`` must
    equal the raw-form overlap ``score_citations`` produces for that answer,
    and both must score HIGH (the echo and raw shapes must not diverge).

    RED at base: no unescape on the model side, so the echo answer scores
    the low escaped-token overlap (0.25) instead of the raw-form 0.8 —
    ``assert 0.25 == 0.8 ...``.
    """
    memory = MemoryRecord(
        id=1,
        content=MEMORY_BODY,
        category=None,
        tags=None,
        source="test",
        created_at=None,
        updated_at=None,
    )
    vector_results = [
        {"text": "Revenue reports cover EMEA.", "file_id": "file1", "metadata": {"source_file": "doc.md"}, "score": 0.9},
    ]

    natural_answer = unescape(MODEL_ANSWER)
    expected_raw = score_citations(
        natural_answer, [], support_texts_by_label={"M1": MEMORY_BODY}
    ).citation_confidence["M1"]
    # Discriminator sanity: the raw-form score must be the HIGH overlap, and
    # an un-normalized echo answer vs the raw body must diverge from it —
    # otherwise the check could not distinguish normalized from raw scoring.
    assert expected_raw > 0.7
    echo_expected = score_citations(
        MODEL_ANSWER, [], support_texts_by_label={"M1": MEMORY_BODY}
    ).citation_confidence["M1"]
    assert echo_expected < expected_raw

    # Echo-shaped answer (model copies the escaped fragments verbatim).
    engine_echo = _build_engine(memory=memory, response=MODEL_ANSWER, vector_results=vector_results)
    done_echo = await _run_done(engine_echo)
    assert [m["memory_label"] for m in done_echo["memories_used"]] == ["M1"]
    score_echo = done_echo["citation_confidence"]["M1"]

    # Natural-character answer (model normalizes entities — the observed
    # real-model behavior from the PRR-003 review probes).
    natural_answer = unescape(MODEL_ANSWER)
    engine_raw = _build_engine(memory=memory, response=natural_answer, vector_results=vector_results)
    done_raw = await _run_done(engine_raw)
    assert [m["memory_label"] for m in done_raw["memories_used"]] == ["M1"]
    score_raw = done_raw["citation_confidence"]["M1"]

    # AC: BOTH answer shapes score exactly the raw-form overlap — scoring is
    # independent of which form the model emitted.
    assert score_echo == pytest.approx(expected_raw)
    assert score_raw == pytest.approx(expected_raw)
