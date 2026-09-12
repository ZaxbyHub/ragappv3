"""Regression pins for review RP-001/RP-002 (PR #576 posted review round).

RP-001: the watch loop must consume (_wake_event.clear()) the wake INSIDE the
loop body immediately after the wait returns. Clearing after the loop (or only
in start()) lets stop()'s task-cancel path skip the reset, so the next
reconcile-wake leaves every wait resolving instantly — an unbounded busy loop
of scan_once() (independently reproduced at 13,757 scans / 0.5s).

RP-002: ordinary 4xx responses must NOT charge circuit breakers — only outage
statuses (5xx/429) do.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402


class TestWakeEventConsumedInLoop(unittest.IsolatedAsyncioTestCase):
    """RP-001 — a reconcile wake must not survive into subsequent waits."""

    async def test_wake_is_consumed_each_iteration(self):
        from app.services.file_watcher import FileWatcher

        watcher = FileWatcher(processor=MagicMock())
        loop = asyncio.get_running_loop()
        watcher._loop = loop
        watcher._running = True

        scan_calls = {"n": 0}

        async def fake_scan():
            scan_calls["n"] += 1
            watcher._wake_event.set()  # simulate reconcile wake each cycle
            if scan_calls["n"] >= 5:
                watcher._shutdown_event.set()

        watcher.scan_once = fake_scan
        watcher._shutdown_event.clear()
        watcher._wake_event.clear()

        await asyncio.wait_for(watcher._watch_loop(), timeout=10)

        # Five cycles ran; the loop must have honored each wait rather than
        # busy-spinning through thousands of instant iterations.
        self.assertEqual(scan_calls["n"], 5)
        self.assertFalse(
            watcher._wake_event.is_set(),
            "wake event must be consumed inside the loop body",
        )


class TestRerankingBreakerIgnoresOrdinary4xx(unittest.IsolatedAsyncioTestCase):
    """RP-002 — ordinary 4xx must not charge reranking_cb."""

    async def test_404_response_does_not_charge_breaker(self):
        from app.services import reranking as rr
        from app.services.circuit_breaker import CircuitBreakerState

        service = rr.RerankingService(
            reranker_url="http://reranker.local", reranker_model="", top_n=3
        )
        # _rerank_via_endpoint runs SSRF validation in a worker thread; stub
        # it like test_reranking_contract.py does (offline determinism).
        patcher = patch("app.services.reranking.assert_url_safe", lambda url: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        calls = {"n": 0}

        async def handler(request):
            calls["n"] += 1
            return httpx.Response(404, text="not found")

        service._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        breaker = rr.reranking_cb
        breaker._state = CircuitBreakerState.CLOSED
        breaker._fail_counter = 0
        try:
            chunks, success = await service.rerank(
                "query", [{"text": "a"}, {"text": "b"}]
            )
            self.assertEqual(
                breaker._fail_counter,
                0,
                "ordinary 4xx must not charge the reranking breaker",
            )
            self.assertEqual(calls["n"], 1)
            self.assertFalse(
                success, "a 404 rerank must degrade gracefully, not succeed"
            )
            self.assertEqual(chunks[0]["text"], "a")  # original order preserved
        finally:
            breaker._state = CircuitBreakerState.CLOSED
            breaker._fail_counter = 0
            await service._http_client.aclose()


if __name__ == "__main__":
    unittest.main()
