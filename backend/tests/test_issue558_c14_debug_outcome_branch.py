"""Issue #558 C14 (PRR-002 follow-up): pin the DEBUG-breadcrumb branch of the
chat-stream heartbeat cleanup.

The frozen C4 check (``test_issue558_c14_no_unretrieved_task_error.py``) pins
the StopAsyncIteration path, which is silently discarded. This test pins the
OTHER done-task branch: a follow-up ``__anext__`` task that completes with a
REAL (non-StopAsyncIteration) exception must be retrieved in the ``finally``
(so asyncio logs no "Task exception was never retrieved" ERROR) and must keep
a DEBUG breadcrumb on the chat-route logger.

Capture uses a dedicated handler attached directly to the route logger: the
emission happens while the stream generator unwinds through asyncgen
finalization, and pytest caplog phase semantics do not reliably retain it for
in-test assertions.
"""

import asyncio
import gc
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import chat as chat_routes


class _ErrorThenRaiseEngine:
    """Yields an error chunk; the follow-up __anext__ raises a real exception."""

    async def query(self, *args, **kwargs):
        yield {"type": "content", "content": "partial"}
        yield {"type": "error", "message": "boom", "code": "X"}
        raise RuntimeError("provider exploded after the error chunk")


class _CollectingHandler(logging.Handler):
    def __init__(self, level=logging.DEBUG):
        super().__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.mark.asyncio
async def test_real_outcome_of_abandoned_task_is_debug_logged():
    route_logger = logging.getLogger("app.api.routes.chat")
    collector = _CollectingHandler()
    old_level = route_logger.level
    route_logger.addHandler(collector)
    route_logger.setLevel(logging.DEBUG)
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_collector = _CollectingHandler(level=logging.ERROR)
    asyncio_logger.addHandler(asyncio_collector)
    try:
        engine = _ErrorThenRaiseEngine()
        resp = chat_routes.stream_chat_response("q", [], engine)
        # One loop tick between frames -- the same real-SSE-writer pacing the
        # C4 check uses; it lets the follow-up __anext__ task run to
        # completion (here: with a RuntimeError) during the error branch's
        # two yields.
        frames = []
        async for frame in resp.body_iterator:
            frames.append(frame)
            await asyncio.sleep(0)
        # The outer generator returns as soon as the inner terminal done
        # frame passes through, leaving the inner suspended at its last
        # yield -- so the cleanup finally (with the DEBUG breadcrumb) runs
        # during asyncgen finalization. Close explicitly, then give the
        # finalizer loop turns.
        await resp.body_iterator.aclose()
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The client contract is unchanged: error and done frames arrive.
        joined = "".join(frames)
        assert '"type": "error"' in joined
        assert '"type": "done"' in joined

        # No spurious asyncio crash-looking ERROR.
        never_retrieved = [
            r
            for r in asyncio_collector.records
            if "Task exception was never retrieved" in r.getMessage()
        ]
        assert never_retrieved == []

        # The real outcome keeps its DEBUG breadcrumb (discriminating
        # assertion: deleting the DEBUG branch or the isinstance filter makes
        # this fail).
        breadcrumbs = [
            r
            for r in collector.records
            if r.levelno == logging.DEBUG
            and "Discarded outcome" in r.getMessage()
            and "RuntimeError" in r.getMessage()
        ]
        assert breadcrumbs, (
            "expected a DEBUG breadcrumb for the non-StopAsyncIteration "
            "outcome; collector saw " + repr(collector.records)
        )
    finally:
        route_logger.removeHandler(collector)
        route_logger.setLevel(old_level)
        asyncio_logger.removeHandler(asyncio_collector)
