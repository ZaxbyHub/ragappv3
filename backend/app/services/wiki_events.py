"""In-process pub/sub for wiki compile job events.

Publishers (the WikiCompileProcessor) call ``publish(vault_id, event)`` after
a job reaches a terminal state. Subscribers (the SSE handler at
``GET /api/wiki/events``) call ``subscribe(vault_id)`` to receive a per-client
``asyncio.Queue`` of events scoped to that vault.

All operations run on the FastAPI event loop. The processor's poll loop and
the SSE handlers share the same loop, so ``put_nowait`` is safe without
cross-thread marshalling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Bounded per-subscriber queue. Wiki job throughput is low; 64 events is
# generous headroom for a slow client and prevents unbounded memory growth.
_QUEUE_MAX = 64


class WikiEventBus:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = {}

    def subscribe(self, vault_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subs.setdefault(vault_id, set()).add(q)
        return q

    def unsubscribe(self, vault_id: int, q: asyncio.Queue) -> None:
        subs = self._subs.get(vault_id)
        if subs is None:
            return
        subs.discard(q)
        if not subs:
            self._subs.pop(vault_id, None)

    def publish(self, vault_id: int, event: dict) -> None:
        subs = self._subs.get(vault_id)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest event to make room. SSE clients
                # always refetch on any event, so a dropped event is not fatal.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    logger.debug("wiki event bus: dropped event for vault %d", vault_id)

    def publish_page_change(
        self,
        vault_id: int,
        page_id: int,
        action: str,
        user_id: Optional[int] = None,
    ) -> None:
        """Publish a wiki page change event (created/updated/deleted)."""
        event: dict = {
            "type": "page_change",
            "page_id": page_id,
            "action": action,
        }
        if user_id is not None:
            event["user_id"] = user_id
        self.publish(vault_id, event)

    def publish_claim_change(
        self,
        vault_id: int,
        claim_id: int,
        action: str,
        user_id: Optional[int] = None,
    ) -> None:
        """Publish a wiki claim change event (created/updated/deleted)."""
        event: dict = {
            "type": "claim_change",
            "claim_id": claim_id,
            "action": action,
        }
        if user_id is not None:
            event["user_id"] = user_id
        self.publish(vault_id, event)


_bus: Optional[WikiEventBus] = None


def get_wiki_event_bus() -> WikiEventBus:
    global _bus
    if _bus is None:
        _bus = WikiEventBus()
    return _bus
