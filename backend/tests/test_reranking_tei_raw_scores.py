"""TEI rerank raw-score protocol tests (issue #511, RERANK-004).

Covers the acceptance behavior of check C6:

- The client must request raw scores (payload key ``raw_scores`` == True) so
  the returned values are logits, then convert them with a sigmoid EXACTLY
  ONCE — the protocol is explicit and independent of the server's default
  normalization config.
- Ordering, index mapping, and the /rerank route are unchanged.
- Overflow logits still hit the sigmoid guard (1.0 / 0.0).
- A server that ignores raw_scores and returns normalized scores is still
  converted exactly once (documented client-side contract).
"""

import math
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

from app.services.reranking import RerankingService

RERANKER_URL = "http://reranker.local"
RAW_RESPONSE = [
    {"index": 1, "score": 2.0},
    {"index": 0, "score": -1.0},
]
EXPECTED_IDX1 = 1.0 / (1.0 + math.exp(-2.0))   # single sigmoid of 2.0  ~0.8808
EXPECTED_IDX0 = 1.0 / (1.0 + math.exp(1.0))    # single sigmoid of -1.0 ~0.2690


class FakeResponse:
    def __init__(self, status_code, text="", json_body=None):
        self.status_code = status_code
        self.text = text
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("fake HTTP %d" % self.status_code)


class FakeTEIClient:
    """Records the rerank payload; models the TEI server score contract.

    Default TEI config returns sigmoid-normalized 0..1 scores; raw logits
    are returned only when the client requests them via raw_scores=true.
    """

    def __init__(self, raw_response=None):
        self.requests = []
        self._raw_response = raw_response or RAW_RESPONSE

    async def post(self, url, json=None):
        payload = dict(json) if isinstance(json, dict) else {}
        self.requests.append((url, payload))
        if payload.get("raw_scores") is True:
            body = [dict(r) for r in self._raw_response]
        else:
            body = [
                {"index": r["index"], "score": 1.0 / (1.0 + math.exp(-r["score"]))}
                for r in self._raw_response
            ]
        return FakeResponse(200, json_body=body)


def _close(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol


def _service_with_fake(fake):
    service = RerankingService(reranker_url=RERANKER_URL, reranker_model="", top_n=2)
    service._http_client = fake
    return service


@pytest.mark.asyncio
class TestTEIRawScoreProtocol:
    async def test_payload_requests_raw_scores(self):
        fake = FakeTEIClient()
        service = _service_with_fake(fake)

        with patch("app.services.reranking.assert_url_safe", lambda url: None):
            _, success = await service.rerank("q", [{"text": "a"}, {"text": "b"}], top_n=2)

        assert success is True
        assert fake.requests, "no rerank request was recorded"
        _, payload = fake.requests[0]
        assert payload.get("raw_scores") is True, (
            "payload must request raw scores (got keys %s)" % sorted(payload.keys())
        )

    async def test_raw_logits_converted_exactly_once(self):
        fake = FakeTEIClient()
        service = _service_with_fake(fake)

        with patch("app.services.reranking.assert_url_safe", lambda url: None):
            chunks_out, success = await service.rerank(
                "q", [{"text": "a", "id": "0"}, {"text": "b", "id": "1"}], top_n=2
            )

        assert success is True
        first, second = chunks_out[0], chunks_out[1]
        assert first["text"] == "b", "higher raw score (index 1) must come first"
        assert second["text"] == "a"
        assert _close(first["_rerank_score"], EXPECTED_IDX1)
        assert _close(second["_rerank_score"], EXPECTED_IDX0)
        assert first["_rerank_score"] > second["_rerank_score"]

    async def test_request_uses_rerank_route(self):
        fake = FakeTEIClient()
        service = _service_with_fake(fake)

        with patch("app.services.reranking.assert_url_safe", lambda url: None):
            await service.rerank("q", [{"text": "a"}, {"text": "b"}], top_n=2)

        request_url, _ = fake.requests[0]
        assert request_url.endswith("/rerank")

    async def test_overflow_logits_hit_sigmoid_guard(self):
        fake = FakeTEIClient(
            raw_response=[
                {"index": 0, "score": 1000.0},
                {"index": 1, "score": -1000.0},
            ]
        )
        service = _service_with_fake(fake)

        with patch("app.services.reranking.assert_url_safe", lambda url: None):
            chunks_out, success = await service.rerank(
                "q", [{"text": "a"}, {"text": "b"}], top_n=2
            )

        assert success is True
        scores = {c["text"]: c["_rerank_score"] for c in chunks_out}
        assert scores["a"] == 1.0
        assert scores["b"] == 0.0

    async def test_normalized_server_scores_still_converted_once(self):
        """A server ignoring raw_scores returns normalized scores; the client
        still applies exactly one sigmoid (values move toward 0.5, ordering
        is preserved) — documented single-conversion contract."""
        fake = FakeTEIClient()

        service = _service_with_fake(fake)
        # Force the "server ignored the request" branch by stripping the flag
        # server-side: wrap post to pretend raw_scores was not honored.
        original_post = fake.post

        async def ignoring_post(url, json=None):
            payload = dict(json) if isinstance(json, dict) else {}
            payload.pop("raw_scores", None)
            return await original_post(url, json=payload)

        fake.post = ignoring_post

        with patch("app.services.reranking.assert_url_safe", lambda url: None):
            chunks_out, success = await service.rerank(
                "q", [{"text": "a"}, {"text": "b"}], top_n=2
            )

        assert success is True
        scores = {c["text"]: c["_rerank_score"] for c in chunks_out}
        # sigmoid(EXPECTED_IDX1) — exactly one extra conversion of the
        # normalized value the ignoring server returned.
        assert _close(scores["b"], 1.0 / (1.0 + math.exp(-EXPECTED_IDX1)))
        assert _close(scores["a"], 1.0 / (1.0 + math.exp(-EXPECTED_IDX0)))
        assert scores["b"] > scores["a"], "ordering must be preserved"

    async def test_local_path_scores_unchanged_by_protocol_change(self):
        """The local CrossEncoder path still sigmoid-normalizes raw logits once."""
        fake_module = types.ModuleType("sentence_transformers")

        class LocalFakeCrossEncoder:
            def __init__(self, model_id):
                self.model_id = model_id

            def predict(self, pairs):
                return [2.0, -1.0]

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
        scores = {c["text"]: c["_rerank_score"] for c in chunks_out}
        assert _close(scores["a"], EXPECTED_IDX1)
        assert _close(scores["b"], EXPECTED_IDX0)
