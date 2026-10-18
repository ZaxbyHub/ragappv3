"""Issue #715 acceptance check — audit finding T1-32 (degraded retrieval).

``test_maintenance_fallback_is_not_a_decision_abstention``

Contract under test: when retrieval is degraded by an *infrastructure* gate
(maintenance mode), the done payload's ``answer_contract.abstention_basis``
must NOT be ``"decision"``. The maintenance fallback is an availability
signal, not an explicit generation-side abstention decision — marking it
``"decision"`` tells consumers the model *decided* to abstain, which is
false: the engine never offered the model any evidence.

Root cause verified at HEAD (665bbd09):

* ``query()`` sets ``fallback_reason = "RAG index is under maintenance"`` and
  ``vector_results = []`` on the single-query maintenance gate
  (rag_engine.py ~L1803), and yields a fallback chunk with no content
  (~L2024-2033).
* With no sources / memories / wiki / kms in the done payload,
  ``abstention_decision`` is set to ``True`` (rag_engine.py ~L2297-2303).
* ``build_answer_contract`` maps any non-None decision to
  ``abstention_basis="decision"`` (answer_contract.py L77-78).

So today the basis IS ``"decision"``. This check asserts the DESIRED
post-fix behavior and is RED at HEAD with::

    AssertionError: assert 'decision' != 'decision'

Harness reuse: mirrors the ``FakeEmbeddingService`` / ``FakeVectorStore`` /
``FakeMemoryStore`` / ``FakeLLMClient`` pattern of ``tests/test_rag_engine.py``
(the fakes are imported from it), driving the NON-STREAMING ``query()`` path
the same way that module's tests do.
"""

import os
import sys
from typing import cast
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the fakes from the existing RAG-engine unit tests (same import style
# as tests/test_documents_search_ranked.py et al.).
from test_rag_engine import (  # noqa: E402
    FakeEmbeddingService,
    FakeLLMClient,
    FakeMemoryStore,
    FakeVectorStore,
)

from app.config import settings  # noqa: E402
from app.services.embeddings import EmbeddingService  # noqa: E402
from app.services.llm_client import LLMClient  # noqa: E402
from app.services.memory_store import MemoryStore  # noqa: E402
from app.services.rag_engine import RAGEngine  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests
    (same fixture as tests/test_rag_engine.py)."""
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


async def test_maintenance_fallback_is_not_a_decision_abstention():
    """A maintenance-mode degraded answer must not claim abstention_basis
    'decision' — the engine gated retrieval; the model never decided
    anything. T1-32 degraded-retrieval acceptance check (issue #715)."""
    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    # Maintenance mode: every retrieval is gated away, so the answer is
    # generated from an empty context (the degraded-retrieval shape).
    engine.vector_store = cast(VectorStore, FakeVectorStore([]))
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(
        LLMClient,
        FakeLLMClient(
            response="I cannot retrieve any documents while the index is "
            "under maintenance."
        ),
    )
    engine.maintenance_mode = True
    # Keep this test off any developer sqlite DB (visibility filter unused —
    # there is nothing to filter under maintenance).
    engine._get_indexed_file_ids = lambda vault_id: None

    with patch.object(settings, "query_transformation_enabled", False):
        results = [
            msg
            async for msg in engine.query(
                "what is the deployment procedure?", [], stream=False
            )
        ]

    done = [r for r in results if isinstance(r, dict) and r.get("type") == "done"]
    assert done, f"expected a done payload, got: {[r.get('type') for r in results]}"
    contract = done[0].get("answer_contract")
    assert isinstance(contract, dict), (
        f"done payload must carry an answer_contract dict, got: {contract!r}"
    )

    # DESIRED: an infrastructure fallback is not a model abstention decision.
    assert contract["abstention_basis"] != "decision", (
        "maintenance-mode fallback must not be reported as an abstention "
        "'decision' — the model was never offered evidence to decide over "
        f"(answer_contract={contract!r})"
    )
