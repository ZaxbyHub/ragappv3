"""Singleton rerank honesty tests (issue #511, RERANK-003).

Covers the acceptance behavior of check C5:

- A singleton rerank call bypasses scoring, so it must return success=False
  with the chunk unchanged and NO fabricated ``_rerank_score`` (an unscored
  chunk's raw distance must not be labeled with rerank-score semantics
  downstream).
- The multi-item scored path is unchanged: success=True with
  ``_rerank_score`` attached to every returned chunk.
- Empty input is unchanged: ``([], True)``.
"""

import os
import sys
import types
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

import pytest

import app.services.reranking as reranking
from app.services.reranking import RerankingService

PREDICT_CALLS = []  # (n_pairs, logits) actually requested from the fake model


class FakeCrossEncoder:
    """Returns fixed raw logits by pair count; records every predict call."""

    def __init__(self, model_id):
        self.model_id = model_id

    def predict(self, pairs):
        logits = [0.9] if len(pairs) == 1 else [0.9, 0.1]
        PREDICT_CALLS.append((len(pairs), list(logits)))
        return logits


@pytest.fixture(autouse=True)
def fake_sentence_transformers():
    saved = sys.modules.get("sentence_transformers")
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.CrossEncoder = FakeCrossEncoder
    sys.modules["sentence_transformers"] = fake_module
    reranking._reset_local_model_cache()
    PREDICT_CALLS.clear()
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["sentence_transformers"] = saved
        else:
            sys.modules.pop("sentence_transformers", None)
        reranking._reset_local_model_cache()


@pytest.mark.asyncio
class TestSingletonRerankHonesty:
    async def test_singleton_returns_false_without_score(self):
        service = RerankingService(reranker_url="", reranker_model="check-rerank-model")
        single_in = {"text": "only chunk", "file_id": "f1"}

        out, success = await service.rerank("q", [single_in], top_n=5)

        assert success is False, (
            "a scoring bypass must return success=False, not success without a score"
        )
        assert out == [{"text": "only chunk", "file_id": "f1"}], "chunk must be unchanged"
        assert "_rerank_score" not in out[0], "no fabricated _rerank_score"

    async def test_singleton_does_not_invoke_model(self):
        service = RerankingService(reranker_url="", reranker_model="check-rerank-model")
        await service.rerank("q", [{"text": "only chunk"}], top_n=5)
        assert PREDICT_CALLS == [], "singleton bypass must not invoke the CrossEncoder"

    async def test_singleton_via_endpoint_returns_false_without_score(self):
        service = RerankingService(reranker_url="http://reranker.local", reranker_model="", top_n=5)

        class FakeClient:
            async def post(self, url, json=None):
                raise AssertionError("singleton bypass must not call the endpoint")

        service._http_client = FakeClient()
        out, success = await service.rerank("q", [{"text": "only chunk"}], top_n=5)

        assert success is False
        assert out == [{"text": "only chunk"}]
        assert "_rerank_score" not in out[0]

    async def test_multi_item_scored_path_unchanged(self):
        service = RerankingService(reranker_url="", reranker_model="check-rerank-model")

        out, success = await service.rerank("q", [{"text": "a"}, {"text": "b"}], top_n=2)

        assert success is True
        assert len(out) == 2
        for chunk in out:
            assert "_rerank_score" in chunk
        assert PREDICT_CALLS == [(2, [0.9, 0.1])]

    async def test_empty_input_returns_empty_list_true(self):
        service = RerankingService(reranker_url="", reranker_model="check-rerank-model")

        out, success = await service.rerank("q", [], top_n=5)

        assert out == []
        assert success is True

    async def test_singleton_top_n_one_of_many_still_scores(self):
        """top_n=1 with multiple input chunks still goes through the scored path."""
        service = RerankingService(reranker_url="", reranker_model="check-rerank-model")

        out, success = await service.rerank("q", [{"text": "a"}, {"text": "b"}], top_n=1)

        assert success is True
        assert len(out) == 1
        assert "_rerank_score" in out[0]
