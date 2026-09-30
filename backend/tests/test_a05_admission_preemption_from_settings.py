"""Issue #687 acceptance check AC5 (frozen) — foreground preemption must be
reachable from the shipped settings mapping (T1-28-S-04).

The module docstring of app/services/admission.py guarantees that "when a
foreground request is blocked and only background holders occupy the budget,
one local background holder is logically evicted". The #687 fix maps CHAT and
BACKGROUND onto the SHARED "llm" budget key (``from_settings``, sized
``max(chat, background)``), so a CHAT admit contending with a local
BACKGROUND lease for the saturated device capacity now evicts it — this
check pins exactly that (docstring corrected by #827 review PRR-007; the
pre-#687 per-class mapping described here previously made the check RED).
"""

import asyncio
from types import SimpleNamespace

from app.services.admission import AdmissionClass, AdmissionController

CHAT = AdmissionClass.CHAT
BACKGROUND = AdmissionClass.BACKGROUND


def _settings_stub() -> SimpleNamespace:
    """Every attribute AdmissionController.from_settings reads."""
    return SimpleNamespace(
        admission_enabled=True,
        admission_chat_budget=1,
        admission_instant_budget=1,
        admission_embedding_budget=1,
        admission_reranking_budget=1,
        admission_vision_budget=1,
        admission_background_budget=1,
        admission_queue_max_size=8,
        admission_deadline_seconds=None,
        admission_store_url="",
    )


async def test_foreground_preemption_fires_with_from_settings_budgets():
    """AC5: with a controller built by ``AdmissionController.from_settings``,
    a foreground CHAT admit contending with a local BACKGROUND lease for
    saturated device capacity must mark that background lease revoked
    WITHOUT cancelling its task.

    The chat budget (1) is free, so at base the CHAT admit simply acquires
    its own "chat" key and never looks at the BACKGROUND lease parked on the
    separate "background" key: ``bg_lease.revoked`` stays False.
    """
    ctrl = AdmissionController.from_settings(_settings_stub())

    box = {}

    async def background_work():
        async with ctrl.admit(BACKGROUND) as lease:
            box["lease"] = lease
            await asyncio.Event().wait()  # live task; never cancelled

    bg_task = asyncio.create_task(background_work())
    for _ in range(100):  # let the background lease acquire
        await asyncio.sleep(0)
        if "lease" in box:
            break
    assert "lease" in box, "harness: background lease never acquired"
    bg_lease = box["lease"]
    assert bg_lease.revoked is False, "harness: lease must start unrevoked"

    completed = []

    async def chat_turn():
        async with ctrl.admit(CHAT):  # foreground, default
            completed.append(True)

    chat_task = asyncio.create_task(chat_turn())
    for _ in range(50):  # yield control so the CHAT admit runs to the end
        await asyncio.sleep(0)

    try:
        assert bg_lease.revoked is True
    finally:
        for task in (chat_task, bg_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(chat_task, bg_task, return_exceptions=True)
