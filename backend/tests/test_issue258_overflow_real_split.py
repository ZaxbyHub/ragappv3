"""Issue #258 (E2) acceptance checks — AC4 / TEST-007: overflow-order via the
REAL split path.

Phase 2.5 CHECKS ONLY (tier L). TEST-007's defect: the overflow-order tests in
``test_ingestion_performance.py`` patch ``_embed_batch_with_retry`` ITSELF —
the method whose overflow recovery (``_handle_overflow_retry``) is the named
contract — and then assert ``len(embeddings) >= 0``, a tautology that passes
even when every embedding is None or reordered.

This node injects the overflow at the HTTP boundary instead, mirroring the
master ``embeddings.py`` request path:

- the REAL ``EmbeddingService._embed_batch_with_retry`` runs (payload build,
  circuit-breaker-wrapped POST, status/parse handling);
- the HTTP layer is a ``httpx.MockTransport`` on a real ``httpx.AsyncClient``:
  requests carrying MORE than ``_MAX_TEXTS_PER_REQUEST`` texts get a 500 whose
  body matches llama.cpp token-overflow pattern 1 ("input (N tokens) is too
  large") so production treats it as overflow; smaller requests get a 200
  with deterministic PER-TEXT vectors (TEI raw-array shape);
- the REAL ``_handle_overflow_retry`` recursion therefore executes: 20 texts
  -> 500 -> split -> 10+10 -> 500 -> split -> 5+5 -> 200.

Assertions are per-text EXACT (order + content + uniqueness), so an all-None
result or a reversed/duplicated split fails. The two named mutants
(all-None vectors, reversed vectors) are exercised as sub-conditions of the
exact per-text equality and are formally probed in Phase 4.5.

Measured class at base a543361: PRESERVING-of-production (expected green at
base — the production split logic is correct; the defect was the tautological
test, removed in Phase 4).
"""

import json
import os
import sys
import types
import unittest
from unittest.mock import patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Per-file optional-dependency stubs (load-bearing for CI — see
# docs/engineering/testing.md section 2).
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.services.embeddings import EmbeddingService, _EmbeddingRequestConfig

_N_TEXTS = 20
_MAX_TEXTS_PER_REQUEST = 5  # server "batch size limit" enforced by the mock


def _vector_for(text: str) -> list:
    """Deterministic per-text vector: distinct texts get distinct vectors."""
    signature = [ord(c) for c in text]
    return [float(len(text))] + [float((i * 131 + s * 31) % 997) for i, s in enumerate(signature)]


def _overflow_body(n_texts: int) -> str:
    """A real llama.cpp-style token overflow error body."""
    tokens = n_texts * 1024
    return (
        f"{{\"error\":{{\"message\":\"input ({tokens} tokens) is too large, "
        f"current batch size exceeds the context window\","
        f"\"code\":500}}}}"
    )


class _OverflowServer:
    """Stateful mock embedding server: overflow above N texts, else 200."""

    def __init__(self) -> None:
        self.overflow_responses = 0
        self.ok_responses = 0
        self.served_batches: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        texts = payload.get("inputs", [])
        self.served_batches.append([t[:24] for t in texts])
        if len(texts) > _MAX_TEXTS_PER_REQUEST:
            self.overflow_responses += 1
            return httpx.Response(
                500, text=_overflow_body(len(texts)), headers={"content-type": "application/json"}
            )
        self.ok_responses += 1
        return httpx.Response(
            200,
            text=json.dumps([_vector_for(t) for t in texts]),
            headers={"content-type": "application/json"},
        )


class TestOverflowRealSplit(unittest.IsolatedAsyncioTestCase):
    """AC4: a successful recursive overflow preserves every input, in order."""

    async def test_real_split_preserves_all_unique_inputs_in_order(self) -> None:
        # Constructor seam, mirroring the focused embeddings suites: the
        # startup URL safety probe and live settings are stubbed (the request
        # itself runs against a MockTransport with an explicit frozen config).
        with patch("app.services.embeddings.assert_url_safe"), patch(
            "app.services.embeddings.settings"
        ) as mock_settings:
            mock_settings.ollama_embedding_url = "http://127.0.0.1:11434/api/embeddings"
            mock_settings.embedding_model = "issue-258-check"
            mock_settings.embedding_doc_prefix = ""
            mock_settings.embedding_query_prefix = ""
            mock_settings.embedding_batch_max_retries = 3
            mock_settings.embedding_batch_min_sub_size = 1
            service = EmbeddingService()
        try:
            server = _OverflowServer()
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(server.handler)
            )
            try:
                config = _EmbeddingRequestConfig(
                    url="http://embedding.test/embed",
                    mode="tei",
                    ollama_style=None,
                    model="issue-258-check",
                    doc_prefix="",
                    query_prefix="",
                )
                texts = [
                    f"issue-258 overflow regression text #{i:02d} — unique payload"
                    for i in range(_N_TEXTS)
                ]

                embeddings = await service._embed_batch_with_retry(
                    client,
                    texts,
                    max_retries=5,
                    min_sub_size=2,
                    retry_count=0,
                    config=config,
                )

                # The HTTP-level injection really triggered the overflow path.
                self.assertGreaterEqual(server.overflow_responses, 1)
                self.assertGreaterEqual(server.ok_responses, 2)

                # AC4 CHECK — the real recursive split did not return exactly
                # one embedding per input, in order, per-text exact.
                print("AC4 CHECK: FAIL — real split did not return one exact embedding per input in order")
                self.assertIsInstance(embeddings, list)
                self.assertEqual(len(embeddings), _N_TEXTS)
                for i, (text, vector) in enumerate(zip(texts, embeddings)):
                    self.assertIsInstance(vector, list, f"vector {i} is not a list")
                    self.assertTrue(
                        all(isinstance(v, float) for v in vector),
                        f"vector {i} contains non-float values",
                    )
                    self.assertEqual(
                        vector,
                        _vector_for(text),
                        f"vector {i} does not match its input text (order or content corrupted)",
                    )

                # All 20 embeddings are pairwise unique (per-text content
                # equality plus distinct inputs imply distinct vectors).
                self.assertEqual(
                    len({tuple(v) for v in embeddings}), _N_TEXTS,
                    "duplicate embeddings returned by the split",
                )
            finally:
                await client.aclose()
        finally:
            await service.close()


if __name__ == "__main__":
    unittest.main()
