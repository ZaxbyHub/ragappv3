"""TEI rerank over-cap batch acceptance check for issue #719 (AC4).

Feedback round (PRR-009 / PRR-010, review run pr984-20261019): the original
fake scored every batch by its LOCAL index, so batch 1 always lost to batch 0
and the cross-batch merge — the entire point of the fix — was unobservable
(dropping the global offset mapping passed every rerank suite). The fake now
scores by GLOBAL relevance with the best candidates in the SECOND batch, and
the returned order and normalized score range are both asserted.
"""

import json
import math
from unittest.mock import patch

import httpx
import pytest

from app.services.reranking import RerankingService

BATCH_CAP = 32


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.reranking.assert_url_safe", lambda url: None):
        yield


class TestOverCapBatchSplit:
    @pytest.mark.asyncio
    async def test_over_cap_candidate_set_still_reranks(self):
        """A candidate set larger than TEI's per-request batch cap must
        still finish as a successful, fully-scored rerank."""
        batch_sizes: list = []
        # Global relevance: the seven best candidates are the LAST seven of
        # the 40, i.e. they live in the second batch. A correct merge must
        # return them; an implementation that drops batches, drops the
        # per-batch offset, or skips the global sort cannot.
        total = 40
        relevance = {i: float(i) for i in range(total)}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode())
            texts = payload.get("texts", [])
            batch_sizes.append(len(texts))
            if len(texts) > BATCH_CAP:
                # TEI's real client-batch validation rejects oversized
                # posts wholesale.
                return httpx.Response(
                    422, json={"error": "batch too large"}, request=request
                )
            # TEI-shaped scores for whatever batch size arrives. The batch's
            # global range is recoverable from the texts themselves, which is
            # how a real endpoint-independent harness scores per request.
            scored = []
            for i, text in enumerate(texts):
                global_idx = int(text.split()[-1])
                scored.append({"index": i, "score": relevance[global_idx]})
            return httpx.Response(200, json=scored, request=request)

        svc = RerankingService(
            reranker_url="http://reranker.local", reranker_model="", top_n=7
        )
        svc._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            reranked, success = await svc.rerank(
                "query",
                [{"text": f"chunk {i}"} for i in range(total)],
                top_n=7,
            )
        finally:
            await svc._http_client.aclose()

        # The success assert is written with `==` so the frozen base
        # failure line reads exactly: assert False == True
        assert success == True  # noqa: E712
        assert len(reranked) == 7
        assert all("_rerank_score" in chunk for chunk in reranked)
        # Evidence trail: the 40-candidate set was split across batches.
        assert batch_sizes == [BATCH_CAP, total - BATCH_CAP]
        # PRR-009: the cross-batch merge must return the globally best
        # candidates, in descending relevance order. With the best seven at
        # global indices 33..39 a merge that drops the second batch, drops
        # the offset mapping, or skips the global sort fails here.
        assert [c["text"] for c in reranked] == [
            f"chunk {i}" for i in range(39, 32, -1)
        ]
        scores = [c["_rerank_score"] for c in reranked]
        assert scores == sorted(scores, reverse=True)
        # PRR-010: the >cap path must apply the same single sigmoid as the
        # at-cap path, so every score is a normalized 0..1 value rather than
        # a raw logit (the fake's relevance values are 33..39, where a raw
        # return would read far outside 0..1).
        assert all(0.0 <= s <= 1.0 for s in scores)
        assert all(math.isfinite(s) for s in scores)
        # A monotonic transform of the raw relevance: strictly descending
        # and saturated near 1.0, which is what sigmoid(33..39) produces.
        assert min(scores) > 0.9

    @pytest.mark.asyncio
    async def test_over_cap_batch_index_validation_is_per_batch(self):
        """PRR-006: each batch validates its returned indices against its own
        length, so an out-of-range index in a LATER batch still falls back
        instead of silently mapping onto the wrong global candidate."""
        total = 40

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode())
            texts = payload.get("texts", [])
            if len(texts) > BATCH_CAP:
                return httpx.Response(
                    422, json={"error": "batch too large"}, request=request
                )
            if int(texts[0].split()[-1]) == BATCH_CAP:
                # Second batch holds 8 items (global range 32..39). Index 8 is
                # a valid GLOBAL index (0 <= 8 < 40) yet out of range for THIS
                # 8-item batch, so only per-batch validation rejects it.
                # Validating against the whole 40-item set instead would pass
                # it and map it onto global 32+8=40 — one past the candidate
                # set, i.e. exactly the silent mis-attachment #716 guarded.
                return httpx.Response(
                    200, json=[{"index": 8, "score": 9.0}], request=request
                )
            return httpx.Response(
                200,
                json=[
                    {"index": i, "score": float(i)} for i in range(len(texts))
                ],
                request=request,
            )

        svc = RerankingService(
            reranker_url="http://reranker.local", reranker_model="", top_n=7
        )
        svc._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            reranked, success = await svc.rerank(
                "query",
                [{"text": f"chunk {i}"} for i in range(total)],
                top_n=7,
            )
        finally:
            await svc._http_client.aclose()

        # The documented (chunks[:n], False) fallback absorbs it.
        assert success is False
        assert all("_rerank_score" not in chunk for chunk in reranked)
