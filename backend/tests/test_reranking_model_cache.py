"""Local reranker model-identity cache tests (issue #511, RERANK-001).

Covers the acceptance behavior of check C3 plus the plan's edge cases:

- Two distinct reranker model identities must never share a CrossEncoder
  instance; a repeat call for an already-loaded identity must not reload.
- The cache is LRU-capped at 2 loaded models: the least recently used
  identity is evicted (its reference dropped), the touched one survives.
- Concurrent first loads for the same identity under the lock load exactly
  one instance (double-check locking).
- A load failure leaves the cache consistent (no poisoned entry) and the
  ImportError message is unchanged.
"""

import asyncio
import math
import os
import sys
import threading
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

LOADS = []  # model ids passed to the fake CrossEncoder constructor, in order


class FakeCrossEncoder:
    """Records its model identity and scores with identity-tagged logits."""

    def __init__(self, model_id):
        LOADS.append(model_id)
        self.model_id = model_id

    def predict(self, pairs):
        # Identity-tagged raw logit so cross-contamination is observable in
        # the service's sigmoid-normalized _rerank_score values.
        return [1.0 + 1.0 * LOADS.index(self.model_id)] * len(pairs)


def _expected_score(model_id: str) -> float:
    """The service must sigmoid-normalize the model's raw logits exactly once."""
    return 1.0 / (1.0 + math.exp(-(1.0 + 1.0 * LOADS.index(model_id))))


@pytest.fixture(autouse=True)
def fake_sentence_transformers():
    """Shadow sentence_transformers with the fake and reset the model cache."""
    saved = sys.modules.get("sentence_transformers")
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.CrossEncoder = FakeCrossEncoder
    sys.modules["sentence_transformers"] = fake_module
    reranking._reset_local_model_cache()
    LOADS.clear()
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["sentence_transformers"] = saved
        else:
            sys.modules.pop("sentence_transformers", None)
        reranking._reset_local_model_cache()


async def _rerank_with(model_id: str):
    """rerank two chunks with a fresh service instance for `model_id`."""
    service = RerankingService(reranker_model=model_id, reranker_url="")
    return await service.rerank("q", [{"text": "a"}, {"text": "b"}], top_n=2)


def _scores_of(chunks):
    return [c.get("_rerank_score") for c in chunks]


# ---------------------------------------------------------------------------
# Model identity (check C3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestModelIdentityCache:
    async def test_distinct_models_load_their_own_identity(self):
        chunks_a, ok_a = await _rerank_with("modelA")
        assert ok_a is True
        assert LOADS == ["modelA"]
        assert all(abs(s - _expected_score("modelA")) < 1e-9 for s in _scores_of(chunks_a))

        chunks_b, ok_b = await _rerank_with("modelB")
        assert ok_b is True
        assert LOADS == ["modelA", "modelB"], "modelB must load as its own identity"
        assert all(abs(s - _expected_score("modelB")) < 1e-9 for s in _scores_of(chunks_b))

    async def test_repeat_model_does_not_reload(self):
        await _rerank_with("modelA")
        await _rerank_with("modelB")
        chunks_a2, ok_a2 = await _rerank_with("modelA")

        assert ok_a2 is True
        assert LOADS == ["modelA", "modelB"], "repeat modelA call must not reload"
        assert all(abs(s - _expected_score("modelA")) < 1e-9 for s in _scores_of(chunks_a2))

    async def test_get_local_model_returns_per_identity_instances(self):
        m1 = reranking._get_local_model("modelX")
        m2 = reranking._get_local_model("modelY")
        m1_again = reranking._get_local_model("modelX")

        assert m1 is not m2
        assert m1 is m1_again
        assert m1.model_id == "modelX"
        assert m2.model_id == "modelY"


# ---------------------------------------------------------------------------
# LRU cap and eviction (RERANK-001 plan edge cases)
# ---------------------------------------------------------------------------


class TestCacheCapEviction:
    def test_third_model_evicts_least_recently_used(self):
        a = reranking._get_local_model("modelA")
        b = reranking._get_local_model("modelB")
        assert reranking._get_local_model("modelA") is a  # touch A (MRU order: B, A)

        c = reranking._get_local_model("modelC")  # evicts B

        assert c is not None
        assert reranking._get_local_model("modelA") is a, "touched model must survive"
        # B was evicted -> re-requesting it triggers a fresh load.
        b_again = reranking._get_local_model("modelB")
        assert b_again is not b
        assert LOADS == ["modelA", "modelB", "modelC", "modelB"]

    def test_cache_is_capped_at_two_models(self):
        for model_id in ("m1", "m2", "m3", "m4"):
            reranking._get_local_model(model_id)
        assert reranking._loaded_local_model_count() == 2

    def test_evicted_model_reference_is_dropped(self):
        reranking._get_local_model("keepA")
        reranking._get_local_model("keepB")
        reranking._get_local_model("newC")
        cached = list(reranking._iter_local_model_ids())
        assert "keepA" not in cached, "LRU entry must be dropped, not just unordered"
        assert set(cached) == {"keepB", "newC"}


# ---------------------------------------------------------------------------
# Locking (RERANK-001 plan edge cases)
# ---------------------------------------------------------------------------


class TestLocking:
    def test_concurrent_first_load_loads_exactly_once(self):
        """N threads racing the first load of one identity get one instance."""
        barrier = threading.Barrier(8)
        results: list = []
        errors: list = []

        def worker():
            try:
                barrier.wait(timeout=10)
                results.append(reranking._get_local_model("shared"))
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        assert LOADS == ["shared"], "concurrent first load must construct exactly once"
        assert all(r is results[0] for r in results)

    def test_load_failure_does_not_poison_the_cache(self):
        original_cross_encoder = FakeCrossEncoder
        calls = {"n": 0}

        class FailingCrossEncoder:
            def __init__(self, model_id):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("weights download exploded")
                original_cross_encoder(model_id)

        sys.modules["sentence_transformers"].CrossEncoder = FailingCrossEncoder
        try:
            with pytest.raises(RuntimeError, match="weights download exploded"):
                reranking._get_local_model("flaky")
            # The failed attempt must not be cached: a retry loads again and
            # succeeds (the constructor above delegates to the real fake on
            # the second call).
            recovered = reranking._get_local_model("flaky")
            assert recovered is not None
            assert calls["n"] == 2
        finally:
            sys.modules["sentence_transformers"].CrossEncoder = original_cross_encoder


# ---------------------------------------------------------------------------
# ImportError contract (unchanged)
# ---------------------------------------------------------------------------


class TestImportErrorContract:
    def test_missing_sentence_transformers_raises_actionable_runtime_error(self):
        saved = sys.modules.pop("sentence_transformers", None)
        # Also block the real package from importing if it IS installed.
        try:
            with patch.dict(sys.modules, {"sentence_transformers": None}):
                with pytest.raises(RuntimeError) as excinfo:
                    reranking._get_local_model("some-model")
            assert "sentence-transformers is not installed" in str(excinfo.value)
            assert "RERANKER_URL" in str(excinfo.value)
        finally:
            if saved is not None:
                sys.modules["sentence_transformers"] = saved
