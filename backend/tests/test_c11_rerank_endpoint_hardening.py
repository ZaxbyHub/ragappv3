"""Issue #716 acceptance checks — rerank endpoint hardening.

C11-7 (span records reranker model): _rerank_via_endpoint records the span
attribute ``gen_ai.request.model`` from ``payload.get("model", "")``, but the
payload it builds never carries a model — the span always records the empty
string instead of the configured reranker_model.

C11-8 (out-of-range index falls back): a TEI response referencing an index
beyond the chunk list raises IndexError out of rerank() (the index→chunk
mapping loop sits after the try/except); it must degrade to success=False
with the original chunks instead of raising.

Fixture pattern copied from tests/test_reranking_tei_raw_scores.py
(FakeTEIClient / _service_with_fake).
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

from app.services.reranking import RerankingService

RERANKER_URL = "http://reranker.local"
RERANKER_MODEL = "bge-reranker-test"
RAW_RESPONSE = [
    {"index": 1, "score": 2.0},
    {"index": 0, "score": -1.0},
]


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
    """Records the rerank payload; models the TEI server score contract."""

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


def _service_with_fake(fake):
    service = RerankingService(
        reranker_url=RERANKER_URL, reranker_model=RERANKER_MODEL, top_n=2
    )
    service._http_client = fake
    return service


async def test_span_records_reranker_model():
    """[issue #716 / C11-7] The gen_ai rerank span must carry the configured
    reranker_model in gen_ai.request.model — today it records the empty
    string because the payload never has a model key."""
    fake = FakeTEIClient()
    service = _service_with_fake(fake)

    with patch("app.services.reranking.start_span") as span_recorder, patch(
        "app.services.reranking.assert_url_safe", lambda url: None
    ):
        _, success = await service.rerank(
            "q", [{"text": "a"}, {"text": "b"}], top_n=2
        )

    assert success is True
    assert span_recorder.called, "the rerank call must open a gen_ai span"
    call = span_recorder.call_args
    attributes = call.kwargs.get("attributes") or {}
    model_attr = attributes.get("gen_ai.request.model")
    assert model_attr == RERANKER_MODEL


async def test_out_of_range_index_falls_back():
    """[issue #716 / C11-8] A TEI response with index 5 against 2 chunks must
    return success=False with the original chunks — today chunks[idx] raises
    IndexError out of rerank(). The call is wrapped so the base failure is an
    assertion, not an error; the variable indirection keeps ruff happy while
    asserting against False."""
    fake = FakeTEIClient(
        raw_response=[
            {"index": 5, "score": 3.0},
            {"index": 0, "score": 1.0},
        ]
    )
    service = _service_with_fake(fake)
    original = [{"text": "a"}, {"text": "b"}]

    chunks_out = None
    success = None
    try:
        with patch("app.services.reranking.assert_url_safe", lambda url: None):
            chunks_out, success = await service.rerank("q", original, top_n=2)
    except IndexError:
        success = None

    expected_success = False
    assert success == expected_success
    assert chunks_out == original
