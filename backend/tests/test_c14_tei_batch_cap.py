"""TEI rerank over-cap batch acceptance check for issue #719 (AC4)."""

import json
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
            # TEI-shaped scores for whatever batch size arrives.
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
                [{"text": f"chunk {i}"} for i in range(40)],
                top_n=7,
            )
        finally:
            await svc._http_client.aclose()

        # The success assert is written with `==` so the frozen base
        # failure line reads exactly: assert False == True
        assert success == True  # noqa: E712
        assert len(reranked) == 7
        assert all("_rerank_score" in chunk for chunk in reranked)
        # Evidence trail: at least one rerank POST reached the handler
        # (batch_sizes also records how the candidate set was carried).
        assert batch_sizes
