"""Issue-#707 feedback-round coverage: the off-loop resolution seam.

PR #881 review findings F-01 (the ``asyncio.to_thread`` wrap was unpinned —
reverting it left every test green) and F-06 (no ``query()``-level coverage of
filter propagation or error propagation), plus F-05 (the no-filter path should
short-circuit BEFORE the executor hop).

These live in their own file rather than in the checkpoint-frozen
``test_c02_metadata_filter_real_pool.py`` so the frozen acceptance checks stay
byte-identical to their pre-implementation anchor.
"""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from app.models.database import SQLiteConnectionPool
from app.services.metadata_filter import ZERO_MATCH_FILTER_EXPR

_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    file_name TEXT,
    email_sender TEXT,
    document_date TEXT
);
-- alice@corp.example exists in BOTH vaults (files 7 and 21) on purpose: an
-- unscoped resolution would match both, so dropping the vault_id argument at
-- the call site cannot survive this seed.
INSERT INTO files (id, vault_id, file_name, email_sender, document_date) VALUES
    (7,  1, 'q1-report.pdf', 'alice@corp.example', '2024-03-10'),
    (9,  1, 'q2-report.pdf', NULL,                 '2024-06-01'),
    (21, 2, 'other-vault.pdf','alice@corp.example','2024-04-01');
"""


class _RecordingPool(SQLiteConnectionPool):
    """A real pool that records the thread each checkout was served on."""

    def __init__(self, db_path, max_size=2):
        super().__init__(db_path, max_size)
        self.checkout_threads: list[int] = []

    def connection(self):
        self.checkout_threads.append(threading.get_ident())
        return super().connection()


@pytest.fixture
def recording_pool(tmp_path):
    db_path = str(Path(tmp_path) / "offloop.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    pool = _RecordingPool(db_path)
    yield pool
    pool.close_all()


class _Store:
    def __init__(self):
        self.filter_exprs: list[str | None] = []

    async def search(self, embedding, limit, vault_id=None, query_text="",
                     hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
        self.filter_exprs.append(filter_expr)
        return [{
            "id": "7_0", "file_id": "7",
            "text": "Paris is the capital of France.",
            "_distance": 0.1, "metadata": {},
        }]

    def get_fts_exceptions(self):
        return 0


class _Embed:
    async def embed_single(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_passage(self, text):
        return [0.1, 0.2, 0.3]


class _Memory:
    def detect_memory_intent(self, text):
        return None


class _LLM:
    base_url = "stub"
    model = "stub"

    def __init__(self):
        self.last_metrics = {}

    async def chat_completion(self, messages, **kw):
        return "The capital of France is Paris, a famous city."

    async def chat_completion_stream(self, messages, **kw):
        yield "The capital of France is Paris, a famous city."


def _engine(store):
    from app.services.rag_engine import RAGEngine

    return RAGEngine(
        embedding_service=_Embed(),
        vector_store=store,
        memory_store=_Memory(),
        llm_client=_LLM(),
        reranking_service=None,
    )


def _mock_settings(mock_settings):
    mock_settings.agentic_rag_enabled = False
    mock_settings.default_chat_mode = "thinking"
    mock_settings.query_transformation_enabled = False
    mock_settings.memory_retrieval_enabled = False
    mock_settings.retrieval_evaluation_enabled = False
    mock_settings.context_distillation_enabled = False
    mock_settings.context_distillation_synthesis_enabled = False
    mock_settings.context_max_tokens = 0
    mock_settings.parent_retrieval_enabled = False
    mock_settings.retrieval_recency_weight = 0.0
    mock_settings.rrf_legacy_mode = False
    mock_settings.exact_match_promote = False
    mock_settings.reranking_enabled = False
    mock_settings.hybrid_search_enabled = False
    mock_settings.hybrid_alpha = 0.6
    mock_settings.maintenance_mode = False
    mock_settings.max_distance_threshold = 1.0
    mock_settings.rag_relevance_threshold = 0.5
    mock_settings.retrieval_top_k = 10
    mock_settings.retrieval_window = 0
    mock_settings.initial_retrieval_top_k = 10
    mock_settings.reranker_top_n = 5
    mock_settings.thinking_max_tokens = 1024
    mock_settings.rag_trace_in_response = False
    mock_settings.kms_enabled = False
    return mock_settings


async def _drive(engine, **query_kwargs):
    """Run one query() turn to completion and return the done chunk."""
    async for chunk in engine.query("what is the capital", [], stream=False, **query_kwargs):
        if chunk.get("type") == "done":
            return chunk
    return None


@pytest.mark.asyncio
async def test_resolution_runs_off_the_event_loop_thread(monkeypatch, recording_pool):
    """F-01: the resolver's blocking pool checkout must NOT run on the event
    loop thread. Before this pin, replacing ``await asyncio.to_thread(...)``
    with a direct synchronous call left the whole suite green, so nothing
    protected the issue #707 constraint "the checkout runs off-loop"."""
    loop_thread = threading.get_ident()
    monkeypatch.setattr(
        "app.models.database.get_pool",
        lambda sqlite_path, max_size=5: recording_pool,
    )

    store = _Store()
    engine = _engine(store)
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.rag_engine.settings"
    ) as mock_settings, __import__(
        "unittest.mock", fromlist=["patch"]
    ).patch(
        "app.services.rag_engine._get_pool", side_effect=RuntimeError("no db")
    ):
        _mock_settings(mock_settings)
        done = await _drive(engine, vault_id=1, metadata_filter={"author": "alice@corp.example"})

    assert done is not None
    assert recording_pool.checkout_threads, "the resolver never checked out a connection"
    for thread_id in recording_pool.checkout_threads:
        assert thread_id != loop_thread, (
            "metadata-filter resolution ran its blocking pool checkout on the "
            "event loop thread — the off-loop wrap was removed"
        )


@pytest.mark.asyncio
async def test_no_filter_turn_never_touches_the_pool(monkeypatch, recording_pool):
    """F-05: an unfiltered turn must short-circuit before the executor hop —
    the resolver returns None without any DB access, so it must not even
    check out a connection."""
    monkeypatch.setattr(
        "app.models.database.get_pool",
        lambda sqlite_path, max_size=5: recording_pool,
    )

    store = _Store()
    engine = _engine(store)
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.rag_engine.settings"
    ) as mock_settings, __import__(
        "unittest.mock", fromlist=["patch"]
    ).patch(
        "app.services.rag_engine._get_pool", side_effect=RuntimeError("no db")
    ):
        _mock_settings(mock_settings)
        done = await _drive(engine, vault_id=1)

    assert done is not None
    assert recording_pool.checkout_threads == [], (
        "an unfiltered turn performed a pool checkout it never needed"
    )


@pytest.mark.asyncio
async def test_author_filter_reaches_search_scoped_to_the_request_vault(
    monkeypatch, recording_pool
):
    """F-06: the resolved expression must actually reach vector_store.search,
    and the vault-2 file that shares the author must be excluded. This pins
    the ``vault_id`` argument being threaded to the resolver — a mutant that
    drops it resolves alice's OTHER vault and lets file 21 through."""
    monkeypatch.setattr(
        "app.models.database.get_pool",
        lambda sqlite_path, max_size=5: recording_pool,
    )

    store = _Store()
    engine = _engine(store)
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.rag_engine.settings"
    ) as mock_settings, __import__(
        "unittest.mock", fromlist=["patch"]
    ).patch(
        "app.services.rag_engine._get_pool", side_effect=RuntimeError("no db")
    ):
        _mock_settings(mock_settings)
        done = await _drive(
            engine, vault_id=1, metadata_filter={"author": "alice@corp.example"}
        )

    assert done is not None
    assert store.filter_exprs, "vector_store.search was never called"
    # alice exists in vault 1 (file 7) and vault 2 (file 21); only the
    # requested vault may resolve.
    assert "file_id IN ('7')" in store.filter_exprs
    assert all("21" not in (expr or "") for expr in store.filter_exprs), (
        "a file from another vault leaked into the resolved filter expression"
    )


@pytest.mark.asyncio
async def test_programming_error_escapes_query_without_searching(monkeypatch):
    """F-06: a signature-drift TypeError from the checkout seam must escape
    query() rather than degrading — the issue's central invariant ("a filter
    must either resolve correctly or the request must fail loudly"). Pinned at
    query() level, not just at the resolver boundary."""
    def _drift(sqlite_path, max_size=5):
        raise TypeError("signature drift")

    monkeypatch.setattr("app.models.database.get_pool", _drift)

    store = _Store()
    engine = _engine(store)
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.rag_engine.settings"
    ) as mock_settings, __import__(
        "unittest.mock", fromlist=["patch"]
    ).patch(
        "app.services.rag_engine._get_pool", side_effect=RuntimeError("no db")
    ):
        _mock_settings(mock_settings)
        with pytest.raises(TypeError):
            await _drive(engine, vault_id=1, metadata_filter={"author": "alice@corp.example"})

    assert store.filter_exprs == [], (
        "retrieval ran despite the filter failing to resolve — the turn must "
        "fail loudly instead of searching with a degraded filter"
    )


@pytest.mark.asyncio
async def test_unavailability_still_degrades_to_sentinel_through_query(
    monkeypatch, recording_pool
):
    """The documented degradation path must survive the off-loop wrap: a
    pool-unavailability RuntimeError still yields the zero-match sentinel and
    a completed turn, not a hard failure."""
    def _unavailable(sqlite_path, max_size=5):
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr("app.models.database.get_pool", _unavailable)

    store = _Store()
    engine = _engine(store)
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.rag_engine.settings"
    ) as mock_settings, __import__(
        "unittest.mock", fromlist=["patch"]
    ).patch(
        "app.services.rag_engine._get_pool", side_effect=RuntimeError("no db")
    ):
        _mock_settings(mock_settings)
        done = await _drive(engine, vault_id=1, metadata_filter={"author": "alice@corp.example"})

    assert done is not None
    assert ZERO_MATCH_FILTER_EXPR in store.filter_exprs
