"""Issue #717 AC6 acceptance check — non-admin llm_metrics redaction.

RED at base (master), GREEN after the fix.

The non-streaming POST /api/chat route forwards the engine done chunk's
``llm_metrics`` verbatim into the response body. When the engine records
the provider endpoint URL inside those metrics, a non-admin member can
read internal infrastructure topology (scheme, host, port, path) straight
from the API response — data the frontend never renders and that only
admins may see.

AC6 (issue #717): WHEN a non-admin user calls ``POST /api/chat`` and the
engine's done chunk carries ``llm_metrics`` with a ``provider_url`` THE
SYSTEM SHALL return ``llm_metrics`` without a ``provider_url`` key.

Harness: TestClient against the real app (``app.main:app``) with
dependency overrides for the engine, the authenticated user, the policy
evaluator and the vector store (model-ready gate). Admission is stubbed
permissive exactly like tests/test_a06_chat_nonstream_done_parity.py —
the closest existing harness for this very route path.
"""

import os
import sys
import types
import unittest
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


class _RecController:
    """Permissive admission recorder (copied from the A06 harness)."""

    def __init__(self):
        self.calls = []

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        yield


def _make_metrics_leaking_engine():
    """Build a fake RAG engine whose done chunk carries llm_metrics.

    The done chunk embeds the internal provider endpoint URL in
    ``llm_metrics`` — the exact payload shape a non-admin must never see
    echoed back by the route.
    """

    async def fake_query(*args, **kwargs):
        yield {"type": "content", "content": "answer"}
        yield {
            "type": "done",
            "sources": [],
            "memories_used": [],
            "wiki_used": [],
            "kms_used": [],
            "llm_metrics": {
                "provider_url": "http://provider.internal:8080/v1",
                "status": "ok",
                "latency_ms": 5,
            },
        }

    class _Engine:
        query = fake_query

    return _Engine()


class TestNonAdminMetricsRedaction(unittest.TestCase):
    """AC6 (issue #717): non-admin /api/chat responses must not leak provider_url."""

    def test_non_admin_chat_response_has_no_provider_url(self):
        """AC6 (issue #717) — RED at base (master), GREEN after the fix.

        WHEN a non-admin user calls POST /api/chat and the engine's done
        chunk carries llm_metrics with a provider_url THE SYSTEM SHALL
        return llm_metrics without a provider_url key.
        """
        from app.api.deps import (
            get_current_active_user,
            get_evaluate_policy,
            get_rag_engine,
            get_vector_store,
        )
        from app.api.routes import chat as chat_module

        engine = _make_metrics_leaking_engine()

        # The model-ready gate requires an initialized, ready vector store.
        ready_vector_store = MagicMock()
        ready_vector_store._ready = True

        async def allow_evaluate(user, resource_type, resource_id, action):
            return True

        # Non-admin member (same dict shape the chat policy-DI suite uses);
        # vault read access is granted by the overridden policy evaluator.
        member_user = {
            "id": 42,
            "username": "member1",
            "email": "member1@example.com",
            "role": "member",
        }

        app.dependency_overrides[get_rag_engine] = lambda: engine
        app.dependency_overrides[get_current_active_user] = lambda: member_user
        app.dependency_overrides[get_evaluate_policy] = lambda: allow_evaluate
        app.dependency_overrides[get_vector_store] = lambda: ready_vector_store

        client = TestClient(app)
        try:
            with patch.object(
                chat_module, "get_admission_controller", lambda: _RecController()
            ):
                response = client.post(
                    "/api/chat",
                    json={"message": "hello", "history": [], "vault_id": 7},
                )
            self.assertEqual(response.status_code, 200, response.text[:400])
            body = response.json()
            self.assertIn("llm_metrics", body)
            self.assertIsInstance(body["llm_metrics"], dict)
            # Redaction must be surgical — the metrics object survives with
            # its non-sensitive fields intact.
            self.assertEqual(body["llm_metrics"].get("status"), "ok")
            self.assertEqual(body["llm_metrics"].get("latency_ms"), 5)
            leaked = "provider_url" in body["llm_metrics"]
            assert leaked == False  # noqa: E712
        finally:
            for dep in (
                get_rag_engine,
                get_current_active_user,
                get_evaluate_policy,
                get_vector_store,
            ):
                app.dependency_overrides.pop(dep, None)


if __name__ == "__main__":
    unittest.main()
