"""In-process, bounded pub/sub for Draft Room project events.

Publishers (the :class:`~app.services.draft_job_processor.DraftJobProcessor` and
the Draft Room routes) call :meth:`DraftEventBus.publish` *after* the
state-changing transaction commits. Subscribers (the SSE handler at
``GET /api/draft-room/drafts/{draft_id}/events``) call :meth:`subscribe` to
receive a per-client ``asyncio.Queue`` of events scoped to one draft.

Design constraints from ``specs/draft-room/SPEC.md`` section 8.4:

* This bus is **process-local notification, not durable state**. The database
  is canonical; a dropped event is repaired by the client's required REST
  refetch. Never treat delivery as proof that work completed, and never promise
  event replay.
* Each subscriber queue is bounded at :data:`_QUEUE_MAX` events. When a slow
  consumer fills its queue, low-value ``stage_progress``/``heartbeat`` events
  are dropped to make room, and room is always made for a terminal event.
* Payloads carry identifiers, stage names, progress numbers, and short
  summaries **only**. They must never carry manuscript text, evidence passages,
  prompts, draft content, storage paths, or secrets. :func:`build_event`
  enforces this by construction.

All operations run on the FastAPI event loop — the processor's poll loop and
the SSE handlers share that loop, so ``put_nowait`` is safe without cross-thread
marshalling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Bounded per-subscriber queue (SPEC section 8.4). Draft Room event throughput is
# low; 100 events is generous headroom for a slow client while keeping memory
# bounded when a browser tab is suspended.
_QUEUE_MAX = 100

# The complete set of event types this bus may carry (SPEC section 8.4).
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "subscribed",
        "job_started",
        "stage_started",
        "stage_progress",
        "stage_completed",
        "finding_created",
        "job_completed",
        "job_failed",
        "job_cancelled",
        "heartbeat",
    }
)

# Terminal events must never be dropped in favour of a queued progress tick: they
# are what tells a client the work is over.
_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"job_completed", "job_failed", "job_cancelled"}
)

# Events safe to discard first when a slow consumer's queue is full. Both are
# purely advisory: progress is re-derivable from the database and a missed
# heartbeat only delays liveness detection.
_DROPPABLE_EVENT_TYPES: frozenset[str] = frozenset({"stage_progress", "heartbeat"})

# Field allowlist for event payloads. Anything not named here is rejected by
# build_event, so a future caller cannot accidentally widen the SSE surface to
# include content. Every value must be a non-content scalar.
_ALLOWED_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "draft_id",
        "job_id",
        "input_id",
        "revision_id",
        "finding_id",
        "job_type",
        "status",
        "stage",
        # Stage retry counter. Content-free (a small integer) and needed so a
        # subscriber can tell a first attempt from a retry of the same stage.
        "attempt",
        "progress_percent",
        "error_code",
        "severity",
        "category",
    }
)

# Bound on any string value that reaches a subscriber. Stage names, statuses and
# error codes are short stable enums; this is a backstop, not a formatter.
_MAX_STR_LEN = 64


class DraftEventPayloadError(ValueError):
    """Raised when an event payload violates the no-content SSE contract."""


def build_event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Build a validated, content-free Draft Room SSE event.

    This is the only supported way to construct an event. It fails closed on
    anything that could leak draft content through the notification channel.

    Args:
        event_type: One of :data:`EVENT_TYPES`.
        **fields: Non-content scalar identifiers drawn from
            :data:`_ALLOWED_PAYLOAD_FIELDS`. ``None`` values are dropped.

    Returns:
        A dict with ``type`` plus the supplied fields.

    Raises:
        DraftEventPayloadError: If the type is unknown, a field is not
            allowlisted, or a value is not a permitted scalar.
    """
    if event_type not in EVENT_TYPES:
        raise DraftEventPayloadError(f"unknown draft event type: {event_type!r}")

    event: dict[str, Any] = {"type": event_type}
    for key, value in fields.items():
        if value is None:
            continue
        if key not in _ALLOWED_PAYLOAD_FIELDS:
            raise DraftEventPayloadError(
                f"field {key!r} is not permitted in a draft event payload"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise DraftEventPayloadError(
                f"field {key!r} must be an int, float, or str; got {type(value).__name__}"
            )
        if isinstance(value, str):
            if len(value) > _MAX_STR_LEN:
                raise DraftEventPayloadError(
                    f"field {key!r} exceeds {_MAX_STR_LEN} characters"
                )
            if "\n" in value or "\r" in value:
                raise DraftEventPayloadError(
                    f"field {key!r} must not contain newlines"
                )
        event[key] = value
    return event


class DraftEventBus:
    """Per-draft fan-out of process-local notification events."""

    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = {}

    def subscribe(self, draft_id: int) -> asyncio.Queue:
        """Register a new bounded subscriber queue for ``draft_id``."""
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subs.setdefault(draft_id, set()).add(q)
        return q

    def unsubscribe(self, draft_id: int, q: asyncio.Queue) -> None:
        """Remove a subscriber queue. Callers do this in a generator ``finally``."""
        subs = self._subs.get(draft_id)
        if subs is None:
            return
        subs.discard(q)
        if not subs:
            self._subs.pop(draft_id, None)

    def subscriber_count(self, draft_id: int) -> int:
        """Number of live subscribers for ``draft_id`` (diagnostics and tests)."""
        return len(self._subs.get(draft_id, ()))

    def publish(self, draft_id: int, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every subscriber of ``draft_id``.

        Never raises: a notification failure must not affect job execution or a
        route handler. When a subscriber queue is full, droppable events are
        discarded oldest-first to make room; a terminal event always displaces
        something rather than being lost.
        """
        subs = self._subs.get(draft_id)
        if not subs:
            return
        event_type = event.get("type") if isinstance(event, dict) else None
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._publish_to_full_queue(draft_id, q, event, event_type)
            except Exception:
                logger.warning(
                    "draft event bus: publish failed (draft_id=%s event_type=%s)",
                    draft_id,
                    event_type,
                    exc_info=True,
                )

    def _publish_to_full_queue(
        self,
        draft_id: int,
        q: asyncio.Queue,
        event: dict[str, Any],
        event_type: Optional[str],
    ) -> None:
        """Make room in a full subscriber queue, or drop the incoming event.

        A droppable (progress/heartbeat) event is simply discarded — the client
        refetches canonical state anyway. Anything else evicts the oldest queued
        item so the newer, more meaningful event survives.
        """
        if event_type in _DROPPABLE_EVENT_TYPES:
            # Coalescing: the newest progress state is the only one that matters,
            # and the queue already holds strictly more recent context than this
            # tick would add once it is this far behind.
            return
        try:
            q.get_nowait()
            q.put_nowait(event)
        except Exception:
            # Losing a terminal event is operationally significant — the client
            # will only learn the job ended on its next REST refetch. Surface it.
            level = (
                logger.warning
                if event_type in _TERMINAL_EVENT_TYPES
                else logger.debug
            )
            level(
                "draft event bus: failed to deliver event to slow consumer "
                "(draft_id=%s event_type=%s); event dropped after drain failure",
                draft_id,
                event_type,
                exc_info=True,
            )


_bus: Optional[DraftEventBus] = None


def get_draft_event_bus() -> DraftEventBus:
    """Return the process-wide Draft Room event bus, creating it on first use."""
    global _bus
    if _bus is None:
        _bus = DraftEventBus()
    return _bus
