"""Issue #687 acceptance check AC8 (frozen) — a queued non-durable stream
must heartbeat while it waits for admission (TQ-budget-all-05).

On the non-durable stream path, ``event_generator`` (chat.py ~L1488) awaits
``admit(AdmissionClass.CHAT)`` BEFORE iterating ``_event_generator_inner()``
(chat.py ~L1523), which owns every heartbeat mechanism — the inner
generator's ``": heartbeat"`` yield (chat.py ~L986) and the CHAT-002 comment
contract. A queued stream therefore sends no bytes while it waits in the
admission queue, so idle proxies with short read timeouts can drop it.

Driven path: the NON-DURABLE ``event_generator`` closure of
``stream_chat_response`` — no durable ids/db_pool are passed, so
``durable_active`` is False.
"""

import asyncio
from unittest.mock import MagicMock

from app.api.routes import chat as chat_module
from app.services.admission import (
    AdmissionClass,
    AdmissionController,
    MemoryAdmissionStore,
)

CHAT = AdmissionClass.CHAT


def _stub_engine():
    engine = MagicMock()

    def _query(*args, **kwargs):
        async def _gen():
            yield {"type": "content", "content": "ok"}
            yield {"type": "done", "sources": [], "memories_used": []}

        return _gen()

    engine.query = _query
    engine.llm_client = None
    return engine


async def test_queued_stream_sends_heartbeats_before_admission(monkeypatch):
    """AC8: a non-durable streaming chat request waiting in the admission
    queue longer than the heartbeat interval must emit SSE heartbeat
    comments (``:`` lines) while it waits.

    ``CHAT_HEARTBEAT_INTERVAL`` is set to 0.05s and the chat budget (1) is
    held by a lease. The non-durable stream generator is read with a 0.3s
    per-chunk timeout until the first timeout; at base NO chunk is ever
    produced before admission, so the heartbeat count is 0 and
    ``assert count >= 1`` fails as ``assert 0 >= 1``.
    """
    monkeypatch.setattr(chat_module, "CHAT_HEARTBEAT_INTERVAL", 0.05)
    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=MemoryAdmissionStore(),
    )
    monkeypatch.setattr(
        chat_module, "get_admission_controller", lambda: ctrl
    )

    holder_cm = ctrl.admit(CHAT)
    await holder_cm.__aenter__()  # hold the only chat slot
    for _ in range(12):
        await asyncio.sleep(0)

    engine = _stub_engine()
    response = chat_module.stream_chat_response("question", [], engine)
    stream = response.body_iterator

    # Fixed-duration read window (#687 amend, critic round 1): the base
    # shape broke on the first per-chunk timeout, which on the fixed tree
    # never happens (heartbeats arrive every interval while queued, so the
    # loop would spin forever). Reading within a bounded wall-clock window
    # keeps the check discriminating on both trees: at base no ':' chunk is
    # ever produced before admission (count 0); on the fixed tree the queued
    # stream heartbeats throughout the window.
    heartbeat_lines = 0
    try:
        loop = asyncio.get_running_loop()
        window_end = loop.time() + 0.4
        while loop.time() < window_end:
            try:
                chunk = await asyncio.wait_for(stream.__anext__(), 0.1)
            except asyncio.TimeoutError:
                continue
            except StopAsyncIteration:
                break
            if chunk.startswith(":"):
                heartbeat_lines += 1
        assert heartbeat_lines >= 1
    finally:
        await stream.aclose()
        await holder_cm.__aexit__(None, None, None)
