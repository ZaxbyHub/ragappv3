"""RAG-DEEP-03 (issue #511 B2 PR2): KMS/wiki scheduling in RAGEngine.query().

Regression tests:

1. Overlap on (``retrieval_kms_overlap`` default): the KMS retrieval task
   is launched alongside the memory task and awaited only where its
   evidence is consumed (prompt building), so the document pipeline
   starts BEFORE KMS finishes (doc_start < kms_finish in a shared ordered
   event log). Wiki retrieval stays awaited BEFORE the raw-RAG gate.
2. Flag off (rollback): the legacy ``asyncio.gather(wiki, kms)`` runs —
   KMS finishes before the document pipeline starts.
3. Wiki-answerable query (high-confidence fresh claim for an entity
   lookup): raw RAG is skipped entirely — ZERO embeddings and ZERO vector
   searches — and the KMS overlap task is still awaited safely before
   prompt building (its evidence reaches ``build_messages``).

The fake KMS retrieval BLOCKS in its ``asyncio.to_thread`` worker (never
the event loop) until the fake vector store's ``search()`` has recorded
"doc_start", guarded by a timeout so the tests can never hang — the same
discriminating fixture as the frozen acceptance check C12.

Fixture patterns follow backend/tests/test_query_orchestration.py and
backend/tests/test_rag_engine.py (fake providers, per-instance engine
setting overrides, ``_get_indexed_file_ids`` stub).
"""

import contextlib
import os
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.__path__ = []
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from app.services.rag_engine import RAGEngine

KMS_WAIT_TIMEOUT_S = 5.0  # deadlock-avoidance guard: never hang


# ------------------------------------------------------------------
# Shared ordered event log + fake providers
# ------------------------------------------------------------------
class EventLog:
    """Thread-safe ordered event log shared across loop + worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: List[str] = []

    def record(self, name: str) -> None:
        with self._lock:
            self.events.append(name)

    def index(self, name: str) -> Optional[int]:
        with self._lock:
            try:
                return self.events.index(name)
            except ValueError:
                return None


class RecordingEmbeddingService:
    def __init__(self) -> None:
        self.calls: List[str] = []
        self._seq = 0

    async def embed_single(self, text: str) -> List[float]:
        self.calls.append(text)
        self._seq += 1
        return [float(self._seq), 0.25, 0.5]

    async def embed_passage(self, text: str) -> List[float]:
        return await self.embed_single(text)


class DocStartVectorStore:
    """Fake vector store whose search() records 'doc_start' and signals it."""

    def __init__(self, log: EventLog, doc_started: threading.Event) -> None:
        self.log = log
        self.doc_started = doc_started
        self.search_calls = 0

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        vault_id=None,
        query_text=None,
        hybrid: bool = False,
        hybrid_alpha: float = 0.5,
        filter_expr=None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        self.search_calls += 1
        self.log.record("doc_start")
        self.doc_started.set()
        return [
            {
                "id": f"doc_chunk_{self.search_calls}",
                "text": "document chunk relevant to the question",
                "file_id": "docfile1",
                "_distance": 0.10,
                "metadata": {},
            }
        ][:limit]

    def get_fts_exceptions(self) -> int:
        return 0


class FakeWikiRetrieval:
    """Sync retrieve() as production calls it via asyncio.to_thread."""

    def __init__(self, log: EventLog, evidence: Optional[List[Any]] = None) -> None:
        self.log = log
        self.evidence = evidence or []
        self.calls: List[str] = []

    def retrieve(self, query: str, vault_id):
        self.calls.append(query)
        self.log.record("wiki_start")
        self.log.record("wiki_finish")
        return list(self.evidence)


class BlockingKMSRetrieval:
    """Sync retrieve() that blocks (in its to_thread worker) until the
    document pipeline has started, then finishes. Timeout-guarded."""

    def __init__(
        self,
        log: EventLog,
        doc_started: threading.Event,
        block: bool = True,
        evidence: Optional[List[Any]] = None,
    ) -> None:
        self.log = log
        self.doc_started = doc_started
        self.block = block
        self.evidence = evidence if evidence is not None else []
        self.calls: List[str] = []
        self.observed_doc_start = False

    def retrieve(self, query: str, vault_id):
        self.calls.append(query)
        self.log.record("kms_start")
        if self.block:
            self.observed_doc_start = self.doc_started.wait(timeout=KMS_WAIT_TIMEOUT_S)
        self.log.record("kms_finish")
        return list(self.evidence)


class FakeMemoryStore:
    def detect_memory_intent(self, text: str) -> Optional[str]:
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class FakeLLMClient:
    base_url = "http://fake-llm.test"

    def __init__(self, response: str = "kms overlap answer without citations.") -> None:
        self._response = response

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._response

    async def chat_completion_stream(self, messages, **kwargs):
        yield self._response


class FakeTransformer:
    def __init__(self, variants) -> None:
        self._variants = variants

    async def transform(self, query: str):
        return list(self._variants)


class FakePlanner:
    def __init__(self, plan: List[str]) -> None:
        self._plan = plan

    async def plan(self, query: str) -> List[str]:
        return list(self._plan)


class RecordingPromptBuilder:
    """Records the kms/wiki evidence passed to build_messages and returns
    canned messages (the real builder never touches the sentinels)."""

    def __init__(self) -> None:
        self.captured: Dict[str, Any] = {}

    def build_messages(
        self,
        user_input,
        chat_history,
        chunks,
        memories,
        relevance_hint=None,
        wiki_evidence=None,
        kms_evidence=None,
        system_prompt_override=None,
        citation_mode=None,
    ):
        self.captured = {
            "kms_evidence": kms_evidence,
            "wiki_evidence": wiki_evidence,
        }
        return [
            {"role": "system", "content": "You are a test assistant."},
            {"role": "user", "content": user_input},
        ]


def _high_confidence_wiki_evidence():
    """One fresh, active, high-confidence claim (see _raw_rag_required)."""
    from app.services.wiki_retrieval import WikiEvidence

    return WikiEvidence(
        label_placeholder="W1",
        page_id=11,
        claim_id=71,
        title="Deployment region",
        slug="deployment-region",
        page_type="entity",
        claim_text="The primary deployment region is eu-central-1.",
        excerpt="The primary deployment region is eu-central-1.",
        confidence=0.92,
        page_status="published",
        claim_status="active",
        score=0.95,
        score_type="exact_entity",
        freshness=datetime.utcnow().isoformat(),
        source_count=2,
        provenance_summary="2 corroborating documents",
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests."""
    with patch("app.services.embeddings.assert_url_safe"), \
         patch("app.services.llm_client.assert_url_safe"):
        yield


@contextlib.contextmanager
def _settings(**overrides):
    from app.config import settings

    values = {
        "redis_url": "",
        "query_transformation_enabled": True,
        "memory_retrieval_enabled": False,
        "kms_enabled": True,
        "context_distillation_enabled": False,
        "context_max_tokens": 0,
        "parent_retrieval_enabled": False,
        "retrieval_evaluation_enabled": False,
        "agentic_rag_enabled": False,
        "maintenance_mode": False,
        "retrieval_recency_weight": 0.0,
    }
    values.update(overrides)
    with contextlib.ExitStack() as stack:
        for key, value in values.items():
            stack.enter_context(patch.object(settings, key, value))
        yield settings


def _make_engine(emb, vs, llm, wiki, kms, tmp_db: str) -> RAGEngine:
    engine = RAGEngine(
        embedding_service=emb,
        vector_store=vs,
        memory_store=FakeMemoryStore(),
        llm_client=llm,
        wiki_retrieval=wiki,
        kms_retrieval=kms,
        db_path=tmp_db,
    )
    engine.retrieval_top_k = 10
    engine.initial_retrieval_top_k = 10
    engine.reranker_top_n = 10
    engine.hybrid_search_enabled = False
    engine.reranking_enabled = False
    engine.max_distance_threshold = None
    engine.relevance_threshold = None
    engine.retrieval_window = 0
    engine._get_indexed_file_ids = lambda vault_id=None: None

    async def _no_supersession(sources):
        return None

    engine._check_supersession = _no_supersession
    return engine


async def _drive(engine: RAGEngine, user_input: str) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    async for m in engine.query(user_input, [], stream=False, vault_id=1):
        msgs.append(m)
    return msgs


# ------------------------------------------------------------------
# RAG-DEEP-03: KMS overlaps the document pipeline
# ------------------------------------------------------------------
async def test_kms_retrieval_overlaps_document_pipeline(tmp_path):
    """Wiki empty -> raw RAG runs; the document pipeline must START before
    the (blocking) KMS retrieval finishes (doc_start < kms_finish)."""
    log = EventLog()
    doc_started = threading.Event()
    vs = DocStartVectorStore(log, doc_started)
    kms = BlockingKMSRetrieval(log, doc_started, block=True)
    wiki = FakeWikiRetrieval(log, evidence=None)
    emb = RecordingEmbeddingService()
    llm = FakeLLMClient()

    q = "overview of the platform architecture"
    engine = _make_engine(emb, vs, llm, wiki, kms, str(tmp_path / "overlap.db"))
    engine._query_transformers[id(llm)] = FakeTransformer([("original", q)])
    engine._query_planners[id(llm)] = FakePlanner([q])

    with _settings():
        msgs = await _drive(engine, q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"

    order = list(log.events)
    doc_start = log.index("doc_start")
    kms_finish = log.index("kms_finish")
    assert doc_start is not None, f"document pipeline never started; order={order}"
    assert kms_finish is not None, f"KMS retrieval never finished; order={order}"
    assert doc_start < kms_finish, (
        "KMS retrieval serialized AHEAD of the document pipeline — it must "
        f"run concurrently (doc_start < kms_finish); order={order}"
    )
    assert vs.search_calls >= 1, "raw document retrieval must run (wiki evidence empty)"
    assert wiki.calls == [q], f"wiki retrieval must run before the raw-RAG gate; calls={wiki.calls}"
    assert kms.observed_doc_start is True, (
        "KMS finished only after observing the document pipeline start"
    )


# ------------------------------------------------------------------
# RAG-DEEP-03 (rollback flag): legacy gather preserved
# ------------------------------------------------------------------
async def test_kms_overlap_disabled_serializes_before_document_pipeline(tmp_path):
    """retrieval_kms_overlap=False: today's gather is preserved — KMS
    finishes BEFORE the document pipeline starts."""
    log = EventLog()
    doc_started = threading.Event()
    vs = DocStartVectorStore(log, doc_started)
    kms = BlockingKMSRetrieval(log, doc_started, block=False)
    wiki = FakeWikiRetrieval(log, evidence=None)
    emb = RecordingEmbeddingService()
    llm = FakeLLMClient()

    q = "overview of the platform architecture"
    engine = _make_engine(emb, vs, llm, wiki, kms, str(tmp_path / "legacy.db"))
    engine._query_transformers[id(llm)] = FakeTransformer([("original", q)])
    engine._query_planners[id(llm)] = FakePlanner([q])

    with _settings(retrieval_kms_overlap=False):
        msgs = await _drive(engine, q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"

    order = list(log.events)
    kms_finish = log.index("kms_finish")
    doc_start = log.index("doc_start")
    assert kms_finish is not None and doc_start is not None, f"order={order}"
    assert kms_finish < doc_start, (
        "flag off must keep the legacy serialized gather (kms_finish before "
        f"doc_start); order={order}"
    )
    assert wiki.calls == [q]
    assert kms.calls == [q]
    assert vs.search_calls >= 1


# ------------------------------------------------------------------
# RAG-DEEP-03 + RAG-DEEP-01: wiki-answerable skip, KMS task awaited
# ------------------------------------------------------------------
async def test_wiki_answerable_skips_raw_rag_and_awaits_kms_task(tmp_path):
    """High-confidence wiki answer for an entity lookup: zero embeddings,
    zero vector searches, and the KMS overlap task's evidence is awaited
    before prompt building (reaches build_messages)."""
    log = EventLog()
    doc_started = threading.Event()
    vs = DocStartVectorStore(log, doc_started)
    kms_sentinel = object()
    kms = BlockingKMSRetrieval(log, doc_started, block=False, evidence=[kms_sentinel])
    wiki = FakeWikiRetrieval(log, evidence=[_high_confidence_wiki_evidence()])
    emb = RecordingEmbeddingService()
    llm = FakeLLMClient()
    prompt_spy = RecordingPromptBuilder()

    q = "what is the primary deployment region"
    engine = _make_engine(emb, vs, llm, wiki, kms, str(tmp_path / "wikianswer.db"))
    engine._query_transformers[id(llm)] = FakeTransformer(
        [("original", q), ("step_back", "broader infrastructure question"), ("hyde", "passage about regions")]
    )
    engine._query_planners[id(llm)] = FakePlanner([q])
    engine.prompt_builder = prompt_spy

    with _settings():
        msgs = await _drive(engine, q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"

    assert vs.search_calls == 0, (
        "wiki-answerable query must skip raw document retrieval entirely; "
        f"search called {vs.search_calls}x"
    )
    assert emb.calls == [], (
        "wiki-answerable query must compute NO embeddings at all; "
        f"embedded: {emb.calls}"
    )
    assert wiki.calls == [q], "wiki retrieval must run (it decides precedence)"
    assert kms.calls == [q], "KMS retrieval must run (overlap task)"
    assert prompt_spy.captured.get("kms_evidence") == [kms_sentinel], (
        "the KMS overlap task must be awaited before prompt building so its "
        f"evidence is consumed; captured={prompt_spy.captured}"
    )
