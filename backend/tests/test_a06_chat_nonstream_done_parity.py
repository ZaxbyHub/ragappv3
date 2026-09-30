"""Issue #688 defect 1 (A06) — non-stream done-chunk parity, RED at base.

The streaming path forwards ``citation_confidence`` /
``unverifiable_claims`` from the engine's done chunk into its SSE done
payload, but the non-stream path never reads them and ``ChatResponse``
declares no such fields — a non-streaming client silently loses the
citation-honesty metadata that streaming clients receive for the same turn.

Harness: copied from
test_518_review_hardening.py::test_non_stream_llm_metrics_comes_from_engine_done_chunk
(TestClient on a bare FastAPI app with dependency overrides). The stub
engine yields one content chunk and a done chunk carrying
``citation_confidence=0.42`` and ``unverifiable_claims=['c1']``.
"""

import os
import sys
import types
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


def _stub_optional_modules() -> None:
    """Stub missing optional heavy deps so importing app modules is cheap.

    Per-file stubs are load-bearing for CI (requirements-ci.txt omits
    unstructured; lancedb/pyarrow stubs are local fallbacks) — see
    docs/engineering/testing.md section 2.
    """
    for name in ("lancedb", "pyarrow"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)
    try:
        from unstructured.partition.auto import partition  # noqa: F401

        return
    except Exception:
        pass
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    chunking = types.ModuleType("unstructured.chunking")
    chunking.__path__ = []
    title = types.ModuleType("unstructured.chunking.title")
    title.chunk_by_title = lambda *args, **kwargs: []
    documents = types.ModuleType("unstructured.documents")
    documents.__path__ = []
    elements = types.ModuleType("unstructured.documents.elements")
    elements.Element = type("Element", (), {})
    unstructured.partition = partition_pkg
    partition_pkg.auto = auto
    chunking.title = title
    documents.elements = elements
    for name, mod in [
        ("unstructured", unstructured),
        ("unstructured.partition", partition_pkg),
        ("unstructured.partition.auto", auto),
        ("unstructured.chunking", chunking),
        ("unstructured.chunking.title", title),
        ("unstructured.documents", documents),
        ("unstructured.documents.elements", elements),
    ]:
        sys.modules[name] = mod


_stub_optional_modules()


class _RecController:
    """Permissive admission recorder (copied from the 518 harness)."""

    def __init__(self):
        self.calls = []

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        yield


def test_nonstream_response_carries_citation_confidence():
    """The engine done chunk's citation_confidence and unverifiable_claims
    must reach the non-stream /api/chat response body, exactly as they reach
    the streaming done event."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_db, get_rag_engine, get_vector_store
    from app.api.routes import chat as chat_module
    from app.api.routes.chat import get_current_active_user

    async def fake_query(*args, **kwargs):
        yield {"type": "content", "content": "answer"}
        yield {
            "type": "done",
            "sources": [],
            "memories_used": [],
            "citation_confidence": 0.42,
            "unverifiable_claims": ["c1"],
        }

    class _Engine:
        query = fake_query

    ready_vs = MagicMock()
    ready_vs._ready = True

    # Hermetic (same note as the 518 harness): the non-stream path may touch
    # a pooled SQLite connection; the persistence seam is not under test —
    # stub it.
    fake_conn = MagicMock()
    fake_conn.in_transaction = False

    app = FastAPI()
    app.include_router(chat_module.router, prefix="/api")
    app.dependency_overrides[get_rag_engine] = lambda: _Engine()
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1, "username": "u", "email": "u@e", "role": "admin",
    }
    app.dependency_overrides[get_vector_store] = lambda: ready_vs
    app.dependency_overrides[get_db] = lambda: fake_conn

    client = TestClient(app)
    try:
        with patch.object(
            chat_module, "get_admission_controller", lambda: _RecController()
        ):
            response = client.post(
                "/api/chat", json={"message": "hi", "history": []}
            )
        assert response.status_code == 200, response.text[:400]
        assert response.json().get("citation_confidence") == 0.42
        assert response.json().get("unverifiable_claims") == ["c1"]
    finally:
        app.dependency_overrides.clear()
