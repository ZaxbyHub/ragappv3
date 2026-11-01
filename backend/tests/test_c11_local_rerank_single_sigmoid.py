"""Issue #716 acceptance check C11-2 — local CrossEncoder single-sigmoid parity.

sentence-transformers ``CrossEncoder.predict`` applies the model's default
activation (a sigmoid for BGE rerankers) UNLESS the caller passes an explicit
``activation_fn``. The local path in reranking.py calls ``model.predict(pairs)``
and then applies ``_safe_sigmoid`` unconditionally, so the scores are
sigmoided TWICE: sigmoid(4.6) = 0.99 collapses to sigmoid(sigmoid(4.6)) =
0.73. The TEI path already requests raw logits and converts exactly once;
the local path must honor the same single-conversion contract.

Fixture pattern copied from tests/test_reranking_tei_raw_scores.py
(test_local_path_scores_unchanged_by_protocol_change: a fake
``sentence_transformers`` module installed via sys.modules whose predict
applies the sigmoid itself unless activation_fn is passed).
"""

import math
import os
import sys
import types

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

from app.services.reranking import RerankingService

LOGIT_TOP = 4.6
LOGIT_BOTTOM = -4.6


async def test_local_scores_sigmoided_once():
    """[issue #716 / C11-2] logits +4.6/-4.6 through the local CrossEncoder
    path must yield a top _rerank_score of exactly one sigmoid (0.99), not
    sigmoid(sigmoid(...))."""
    fake_module = types.ModuleType("sentence_transformers")

    class LocalFakeCrossEncoder:
        """Mimics sentence-transformers CrossEncoder: predict() applies the
        default (sigmoid) activation UNLESS the caller passes activation_fn
        — activation_fn=False is how a caller requests raw logits."""

        def __init__(self, model_id):
            self.model_id = model_id

        def predict(self, pairs, activation_fn=None):
            raw = [LOGIT_TOP, LOGIT_BOTTOM]
            if activation_fn is None:
                return [1.0 / (1.0 + math.exp(-s)) for s in raw]
            return list(raw)

    fake_module.CrossEncoder = LocalFakeCrossEncoder
    saved = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = fake_module
    try:
        import app.services.reranking as reranking

        reranking._reset_local_model_cache()
        service = RerankingService(reranker_url="", reranker_model="local-m")
        chunks_out, success = await service.rerank(
            "q", [{"text": "a"}, {"text": "b"}], top_n=2
        )
    finally:
        if saved is not None:
            sys.modules["sentence_transformers"] = saved
        else:
            sys.modules.pop("sentence_transformers", None)
        reranking._reset_local_model_cache()

    assert success is True
    top_score = chunks_out[0]["_rerank_score"]
    assert round(top_score, 2) == 0.99
