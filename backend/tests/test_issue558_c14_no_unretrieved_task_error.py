"""Issue #558 acceptance check AC4 (finding C14) — unretrieved task error.

A chat stream whose RAG engine yields an ``error`` chunk and then stops must
NOT leave the heartbeat loop's optimistic follow-up ``__anext__`` task with
an unretrieved exception: asyncio's logger must record no ERROR whose
message contains "Task exception was never retrieved", while the client
still receives the error and done SSE frames (ENH-016 completion marker).

DISCRIMINATING check: RED at base 2a7732a1. The error branch yields two
frames and returns; the follow-up task created at chat.py:937 completes
with StopAsyncIteration during those yields (the consumer suspends between
frames exactly like a real SSE writer), and the ``finally`` guard only
cancels tasks that are still pending — so the finished task's exception is
retrieved by nobody and asyncio logs the ERROR at task destruction.
"""
import asyncio
import gc
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes import chat as chat_routes


class _ErrorThenStopEngine:
    """Engine that streams partial content, fails, then ends."""

    async def query(self, *args, **kwargs):
        yield {"type": "content", "content": "partial"}
        yield {"type": "error", "message": "boom", "code": "X"}
        return


@pytest.mark.asyncio
async def test_error_chunk_then_stop_leaves_no_unretrieved_task_error(caplog):
    """AC4: no asyncio 'Task exception was never retrieved' ERROR after a
    failed stream; error+done frames still reach the client (RED at base)."""
    caplog.set_level(logging.ERROR, logger="asyncio")

    engine = _ErrorThenStopEngine()
    # Non-durable path: no durable args, so body_iterator is the plain
    # event_generator wrapping _event_generator_inner.
    resp = chat_routes.stream_chat_response("q", [], engine)

    frames = []
    async for chunk in resp.body_iterator:
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
        frames.append(text)
        # One loop tick between frames — the pacing a real SSE consumer has
        # between socket writes. This is what lets the optimistic follow-up
        # __anext__ task run to completion during the error branch's two
        # yields, exposing the abandoned-task lifecycle under test.
        await asyncio.sleep(0)
    body = "".join(frames)

    # Client-visible contract preserved: the error frame and the protocol
    # completion marker both arrived.
    assert '"type": "error"' in body
    assert '"type": "done"' in body

    # Give any deferred task destruction (and its __del__ logging) a chance
    # to run inside the capture window.
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()
    for _ in range(2):
        await asyncio.sleep(0)

    unretrieved = [
        r
        for r in caplog.records
        if r.name == "asyncio"
        and r.levelno >= logging.ERROR
        and "Task exception was never retrieved" in r.getMessage()
    ]
    assert not unretrieved, [
        r.getMessage() for r in unretrieved
    ]
