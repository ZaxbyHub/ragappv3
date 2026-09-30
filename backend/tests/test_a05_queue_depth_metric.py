"""Issue #687 acceptance check AC7 (frozen) — the chat queue-wait metric must
record the observed queue depth (P01-SK2-06).

``Telemetry.record_queue_wait`` accepts ``depth: int = 0`` (telemetry.py
L114-L121) and ``AdmissionController.queue_depth`` exists (admission.py
L501-L502), but BOTH production call sites — the durable producer
(chat.py ~L1280) and the non-durable generator (chat.py ~L1519) — pass only
the class and the elapsed seconds, so the exported ``ragapp_queue_depth`` is
always 0.

Driven path: this check drives the NON-DURABLE ``event_generator`` closure of
``stream_chat_response`` (chat.py ~L1488) route-function-direct — no durable
ids/db_pool are passed, so ``durable_active`` is False and the production
``record_queue_wait`` call site at chat.py ~L1519 is exercised against a
recording telemetry stub.
"""

import asyncio
from typing import Any, Dict, List
from unittest.mock import MagicMock

from app.api.routes import chat as chat_module
from app.services.admission import (
    AdmissionClass,
    AdmissionController,
    MemoryAdmissionStore,
)

CHAT = AdmissionClass.CHAT


class _RecordingTelemetry:
    """Stands in for get_telemetry(): captures record_queue_wait kwargs."""

    def __init__(self) -> None:
        self.queue_waits: List[Dict[str, Any]] = []

    def record_queue_wait(self, admission_class, wait_seconds, depth=0):
        self.queue_waits.append(
            {
                "admission_class": str(admission_class),
                "wait_seconds": float(wait_seconds),
                "depth": int(depth),
            }
        )

    # Other telemetry surfaces the stream path touches.
    def record_chat_turn(self, turn_id: str) -> None:
        return None

    def record_first_useful_content(self, turn_id: str, seconds: float) -> None:
        return None


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


async def test_chat_admission_records_real_queue_depth(monkeypatch):
    """AC7: a chat stream admitted after queueing behind another waiter must
    record the observed queue depth with the chat queue-wait metric, not a
    constant 0.

    The chat budget (1) is held by a lease; TWO chat stream requests queue
    behind it route-function-direct with a stub engine. The slot is released
    and both admit in turn. Both production record_queue_wait calls omit
    ``depth`` (chat.py ~L1280 and ~L1519), so the default 0 is recorded and
    ``max(recorded depths) >= 1`` fails as ``assert 0 >= 1``.
    """
    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=MemoryAdmissionStore(),
        queue_max_size=8,
    )
    recorder = _RecordingTelemetry()
    monkeypatch.setattr(chat_module, "get_admission_controller", lambda: ctrl)
    monkeypatch.setattr(chat_module, "get_telemetry", lambda: recorder)

    holder_cm = ctrl.admit(CHAT)
    await holder_cm.__aenter__()  # hold the only chat slot
    for _ in range(12):
        await asyncio.sleep(0)  # the acquire pump settles; calls go quiet

    engine = _stub_engine()

    async def consume(tag: str) -> List[str]:
        response = chat_module.stream_chat_response(f"question-{tag}", [], engine)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    first = asyncio.create_task(consume("one"))
    second = asyncio.create_task(consume("two"))
    for _ in range(25):  # both stream requests queue behind the holder
        await asyncio.sleep(0)
    assert recorder.queue_waits == [], "harness: nobody admitted while held"

    await holder_cm.__aexit__(None, None, None)  # free the slot
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10.0)

    depths = [call["depth"] for call in recorder.queue_waits]
    assert depths, "harness: no chat queue-wait records captured"
    assert max(depths) >= 1
