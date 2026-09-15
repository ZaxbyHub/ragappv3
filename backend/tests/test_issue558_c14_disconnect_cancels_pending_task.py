"""Issue #558 acceptance check AC5 (finding C14) — disconnect cancellation.

A client disconnect mid-stream must still CANCEL a pending follow-up
``__anext__`` task: the cancellation propagates through the heartbeat
loop's ``finally`` into the engine generator's current await. The engine
stub parks on an ``asyncio.Event`` and records whether it observed
``CancelledError`` — the cancel-on-disconnect behavior that any C14 fix
must preserve.

PRESERVING check: GREEN at base 2a7732a1 (the ``finally`` cancels tasks
that are still pending) and must stay green after any correct fix.
"""
import asyncio
import gc
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes import chat as chat_routes


class _ParkingEngine:
    """Streams one content chunk, then parks forever.

    ``saw_cancel`` turns True only if the parked ``Event().wait()`` is
    cancelled — i.e. the pending follow-up __anext__ task was cancelled on
    disconnect and the CancelledError was delivered into the generator.
    """

    def __init__(self) -> None:
        self.saw_cancel = False

    async def query(self, *args, **kwargs):
        yield {"type": "content", "content": "first"}
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise


@pytest.mark.asyncio
async def test_disconnect_midstream_cancels_pending_engine_task():
    """AC5: closing the SSE iterator mid-stream cancels the parked engine
    task (GREEN at base; must remain green after any correct fix)."""
    engine = _ParkingEngine()
    # Non-durable path: body_iterator is the plain event_generator.
    resp = chat_routes.stream_chat_response("q", [], engine)
    it = resp.body_iterator

    # Consume frames up to and including the FIRST CONTENT frame (the very
    # first frame is the leading mode event). Holding the content frame
    # proves the engine's first chunk was retrieved AND the follow-up
    # __anext__ task was created and is now parked on the Event — i.e. the
    # stream is genuinely mid-generation at the moment of disconnect.
    last = None
    for _ in range(6):
        chunk = await it.__anext__()
        last = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
        if '"type": "content"' in last:
            break
    assert last is not None and '"type": "content"' in last

    # Simulate the client disconnect: close the response iterator. The
    # inner generator is finalized asynchronously (asyncgen finalizer), so
    # tick the loop generously to let the finalizer close it and the
    # pending __anext__ task receive its cancellation.
    await it.aclose()
    for _ in range(10):
        await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert engine.saw_cancel is True
