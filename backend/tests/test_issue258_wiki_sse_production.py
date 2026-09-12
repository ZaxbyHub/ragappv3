"""Issue #258 (E2) acceptance checks — AC3 node (a) / TEST-003: Wiki SSE.

Phase 2.5 CHECKS ONLY (tier L). TEST-003's defect: the six SSE tests in
``test_wiki_events.py`` (``TestSSEEventGenerator``) re-implement the event
generator INLINE — the production route
``app.api.routes.wiki.wiki_events_stream`` (whose ``event_generator`` closure
yields the hello frame, published events, and the 15 s keepalive, and whose
``finally`` unsubscribes from the real bus) is never executed by the suite,
so mutating production code cannot fail it.

This node drives the PRODUCTION wiring end to end:

- calls the real ``wiki_events_stream`` route function (the closure + the
  ``StreamingResponse`` it returns are production objects, not replicas);
- the route subscribes through the REAL ``WikiEventBus`` singleton
  (``app.services.wiki_events.get_wiki_event_bus``);
- only the opaque auth/policy seams are stubbed (controlled fixtures, per the
  AC's allowance), and the pooled-connection object is a context-manager stub
  because the pool is released before streaming begins;
- asserts the SSE framing of the hello frame and of a genuinely published
  event, the keepalive comment frame (the 15 s timeout is CLAMPED via a
  patched ``asyncio.wait_for`` — no real 15 s wait), the response media type
  and no-cache headers, and the disconnect cleanup (``aclose`` -> the bus no
  longer holds the subscriber queue).

Measured class at base a543361: PRESERVING-of-production (expected green at
base — the defect was absent coverage, not broken behavior). Phase 4 removes
the inline-replica tests; Phase 4.5 mutation-probes the production generator.
"""

import asyncio
import contextlib
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import app.api.routes.wiki as wiki_routes
import app.services.wiki_events as wiki_events_module
from app.services.wiki_events import get_wiki_event_bus

_VAULT = 1


class _FakePool:
    """Stands in for the request-scoped pooled connection.

    The real route resolves auth + vault read permission on a SHORT-LIVED
    pooled connection and releases it BEFORE streaming starts; the connection
    itself is opaque to the generator, so a null context suffices here.
    """

    def connection(self):
        return contextlib.nullcontext(types.SimpleNamespace())


def _fake_request() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(db_pool=_FakePool())),
        headers={},
        cookies={},
    )


class TestWikiSSEProductionGenerator(unittest.IsolatedAsyncioTestCase):
    """AC3 node (a): the production route's generator/StreamingResponse."""

    async def test_production_route_generator_frames_and_cleanup(self) -> None:
        # Fresh singleton bus so cleanup assertions are isolated.
        original_bus = wiki_events_module._bus
        wiki_events_module._bus = None
        try:
            with patch.object(
                wiki_routes,
                "_resolve_active_user",
                new=AsyncMock(return_value={"id": 1, "username": "sse-check"}),
            ), patch.object(
                wiki_routes,
                "_evaluate_policy",
                new=AsyncMock(return_value=True),
            ):
                response = await wiki_routes.wiki_events_stream(
                    request=_fake_request(), vault_id=_VAULT
                )

                # Production StreamingResponse shape.
                self.assertEqual(response.media_type, "text/event-stream")
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(response.headers["x-accel-buffering"], "no")

                bus = get_wiki_event_bus()
                agen = response.body_iterator

                # 1. Hello frame: positive proof of subscription, SSE-framed.
                hello = await agen.__anext__()
                # While streaming, the REAL bus holds this vault's subscriber.
                self.assertIn(_VAULT, bus._subs)

                # 2. A genuinely published event must come through the real
                #    bus wiring, SSE-framed and byte-exact.
                payload = {
                    "type": "job_completed",
                    "job_id": 77,
                    "vault_id": _VAULT,
                    "status": "completed",
                }
                bus.publish(_VAULT, payload)
                event_frame = await agen.__anext__()

                # 3. Keepalive comment frame when no event arrives within the
                #    (clamped) window — no real 15 s wait.
                real_wait_for = asyncio.wait_for

                async def _clamped(aw, timeout=None):
                    if timeout is not None and timeout > 1.0:
                        timeout = 0.05
                    return await real_wait_for(aw, timeout=timeout)

                with patch.object(asyncio, "wait_for", _clamped):
                    keepalive = await real_wait_for(
                        agen.__anext__(), timeout=5.0
                    )

                # AC3 CHECK — the production event generator does not emit
                # the contract frames (hello / event / keepalive).
                print("AC3 CHECK: FAIL — production event generator does not emit the contract frames")
                self.assertTrue(
                    hello.startswith("data: ") and hello.endswith("\n\n"),
                    f"hello frame not SSE-framed: {hello!r}",
                )
                hello_json = json.loads(hello[len("data: "):].strip())
                self.assertEqual(
                    hello_json, {"type": "subscribed", "vault_id": _VAULT}
                )
                self.assertEqual(
                    event_frame, f"data: {json.dumps(payload)}\n\n"
                )
                self.assertEqual(keepalive, ": keepalive\n\n")

                # 4. Disconnect cleanup: closing the generator must drop the
                #    subscriber from the real bus (the finally block).
                await agen.aclose()
                # AC3 CHECK — disconnect did not unsubscribe from the bus.
                print("AC3 CHECK: FAIL — disconnect did not unsubscribe from the real WikiEventBus")
                self.assertNotIn(_VAULT, bus._subs)
        finally:
            wiki_events_module._bus = original_bus


if __name__ == "__main__":
    unittest.main()
