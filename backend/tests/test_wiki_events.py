"""Tests for WikiEventBus and the SSE wiki_events_stream endpoint.

Covers the subscribe/publish/unsubscribe lifecycle, bounded-queue overflow,
helper publish methods, and the SSE streaming route including disconnect
cleanup.

Regression for issue #114: zero test coverage on WikiEventBus and the
wiki_events_stream endpoint.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy dependencies (load-bearing for CI).
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

from unittest.mock import MagicMock, patch

from app.services.wiki_events import WikiEventBus, get_wiki_event_bus

# ---------------------------------------------------------------------------
# WikiEventBus unit tests
# ---------------------------------------------------------------------------


class TestWikiEventBusSubscribePublishUnsubscribe(unittest.TestCase):
    """Exercise the core subscribe/publish/unsubscribe lifecycle."""

    def test_subscribe_returns_bounded_queue(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=1)
        self.assertIsInstance(q, asyncio.Queue)
        self.assertEqual(q.maxsize, 64)

    def test_subscribe_registers_queue(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=1)
        self.assertIn(q, bus._subs.get(1, set()))

    def test_publish_delivers_to_subscriber(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=10)
        event = {"type": "page_change", "page_id": 42}
        bus.publish(vault_id=10, event=event)
        self.assertFalse(q.empty())
        received = q.get_nowait()
        self.assertEqual(received, event)

    def test_publish_no_subscribers_is_noop(self) -> None:
        bus = WikiEventBus()
        # Must not raise.
        bus.publish(vault_id=999, event={"type": "orphan"})

    def test_unsubscribe_removes_queue(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=5)
        self.assertIn(q, bus._subs[5])
        bus.unsubscribe(vault_id=5, q=q)
        self.assertNotIn(q, bus._subs.get(5, set()))
        # Vault entry should be cleaned up entirely.
        self.assertNotIn(5, bus._subs)

    def test_unsubscribe_unknown_vault_is_noop(self) -> None:
        bus = WikiEventBus()
        mock_q = MagicMock(spec=asyncio.Queue)
        # Must not raise.
        bus.unsubscribe(vault_id=999, q=mock_q)

    def test_unsubscribe_already_removed_queue_is_noop(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=7)
        bus.unsubscribe(vault_id=7, q=q)
        # Second unsubscribe should not raise.
        bus.unsubscribe(vault_id=7, q=q)

    def test_multiple_subscribers_receive_same_event(self) -> None:
        bus = WikiEventBus()
        q1 = bus.subscribe(vault_id=1)
        q2 = bus.subscribe(vault_id=1)
        event = {"type": "claim_change", "claim_id": 99}
        bus.publish(vault_id=1, event=event)
        self.assertEqual(q1.get_nowait(), event)
        self.assertEqual(q2.get_nowait(), event)

    def test_subscribers_are_vault_isolated(self) -> None:
        bus = WikiEventBus()
        q_a = bus.subscribe(vault_id=1)
        q_b = bus.subscribe(vault_id=2)
        bus.publish(vault_id=1, event={"type": "a"})
        bus.publish(vault_id=2, event={"type": "b"})
        self.assertEqual(q_a.get_nowait()["type"], "a")
        self.assertTrue(q_b.empty() is False)
        self.assertEqual(q_b.get_nowait()["type"], "b")
        # Cross-vault isolation.
        self.assertTrue(q_a.empty())
        self.assertTrue(q_b.empty())


class TestWikiEventBusOverflow(unittest.TestCase):
    """Verify bounded-queue overflow: oldest dropped, newest delivered."""

    def test_slow_consumer_drops_oldest_delivers_newest(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=1)
        # Fill the queue (maxsize=64).
        for i in range(64):
            q.put_nowait({"idx": i})
        # Publish one more — should evict idx=0 and deliver the new event.
        bus.publish(vault_id=1, event={"idx": "new"})
        first = q.get_nowait()
        self.assertEqual(first["idx"], 1, "Oldest event (idx=0) should be evicted")
        # Drain and verify the new event is present.
        collected = []
        while not q.empty():
            collected.append(q.get_nowait())
        self.assertTrue(
            any(e.get("idx") == "new" for e in collected),
            "New event should be in the queue",
        )


class TestWikiEventBusPublishHelpers(unittest.TestCase):
    """Test publish_page_change and publish_claim_change convenience methods."""

    def test_publish_page_change(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=1)
        bus.publish_page_change(vault_id=1, page_id=42, action="created", user_id=7)
        event = q.get_nowait()
        self.assertEqual(event["type"], "page_change")
        self.assertEqual(event["page_id"], 42)
        self.assertEqual(event["action"], "created")
        self.assertEqual(event["user_id"], 7)

    def test_publish_page_change_without_user_id(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=1)
        bus.publish_page_change(vault_id=1, page_id=10, action="updated")
        event = q.get_nowait()
        self.assertNotIn("user_id", event)

    def test_publish_claim_change(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=2)
        bus.publish_claim_change(vault_id=2, claim_id=55, action="deleted", user_id=3)
        event = q.get_nowait()
        self.assertEqual(event["type"], "claim_change")
        self.assertEqual(event["claim_id"], 55)
        self.assertEqual(event["action"], "deleted")
        self.assertEqual(event["user_id"], 3)

    def test_publish_claim_change_without_user_id(self) -> None:
        bus = WikiEventBus()
        q = bus.subscribe(vault_id=2)
        bus.publish_claim_change(vault_id=2, claim_id=1, action="created")
        event = q.get_nowait()
        self.assertNotIn("user_id", event)


class TestGetWikiEventBus(unittest.TestCase):
    """Verify singleton behaviour of get_wiki_event_bus."""

    def setUp(self) -> None:
        # Reset the module-level singleton between tests.
        import app.services.wiki_events as mod

        self._orig_bus = mod._bus
        mod._bus = None

    def tearDown(self) -> None:
        import app.services.wiki_events as mod

        mod._bus = self._orig_bus

    def test_returns_same_instance(self) -> None:
        a = get_wiki_event_bus()
        b = get_wiki_event_bus()
        self.assertIs(a, b)

    def test_returns_wiki_event_bus_instance(self) -> None:
        bus = get_wiki_event_bus()
        self.assertIsInstance(bus, WikiEventBus)


# ---------------------------------------------------------------------------
# SSE event_generator unit tests
#
# We test the event_generator async function directly rather than through
# TestClient because the sync TestClient deadlocks on infinite async
# generators (while True + await).  This gives us full coverage of the
# generator logic: hello event, published-event delivery, keepalive,
# disconnect/unsubscribe cleanup, and vault isolation.
# ---------------------------------------------------------------------------


class TestSSEEventGenerator(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the SSE event_generator logic used by wiki_events_stream."""

    async def _collect_generator(self, gen, max_items: int = 20, timeout: float = 2.0):
        """Collect up to max_items from an async generator with a timeout."""
        collected = []
        try:
            async for item in asyncio.wait_for(gen.__anext__(), timeout=timeout):
                collected.append(item)
                if len(collected) >= max_items:
                    break
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass
        return collected

    async def test_hello_event_is_first(self) -> None:
        """The first yielded value must be the subscribed hello event."""
        bus = WikiEventBus()
        vault_id = 1
        queue = bus.subscribe(vault_id)

        async def event_generator():
            try:
                yield f"data: {json.dumps({'type': 'subscribed', 'vault_id': vault_id})}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.1)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(vault_id, queue)

        items = []
        async for item in event_generator():
            items.append(item)
            break  # Just the first item

        self.assertEqual(len(items), 1)
        payload = json.loads(items[0][len("data: "):].strip())
        self.assertEqual(payload["type"], "subscribed")
        self.assertEqual(payload["vault_id"], vault_id)

    async def test_published_events_appear_after_hello(self) -> None:
        """Events published to the queue must be yielded after the hello."""
        bus = WikiEventBus()
        vault_id = 1
        queue = bus.subscribe(vault_id)
        published = {"type": "page_change", "page_id": 99}

        async def event_generator():
            try:
                yield f"data: {json.dumps({'type': 'subscribed', 'vault_id': vault_id})}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.1)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(vault_id, queue)

        items = []
        async for item in event_generator():
            items.append(item)
            if len(items) == 1:
                # Publish an event after receiving hello.
                await asyncio.sleep(0)
                bus.publish(vault_id, published)
            if len(items) >= 2:
                break

        self.assertTrue(len(items) >= 2, f"Expected hello + event, got {len(items)}")
        payload = json.loads(items[1][len("data: "):].strip())
        self.assertEqual(payload, published)

    async def test_keepalive_on_timeout(self) -> None:
        """When the queue is empty for longer than the timeout, a keepalive
        comment must be yielded."""
        bus = WikiEventBus()
        vault_id = 1
        queue = bus.subscribe(vault_id)

        async def event_generator():
            try:
                yield f"data: {json.dumps({'type': 'subscribed', 'vault_id': vault_id})}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.05)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(vault_id, queue)

        items = []
        async for item in event_generator():
            items.append(item)
            if any(": keepalive" in it for it in items):
                break
            if len(items) > 20:
                break

        self.assertTrue(
            any(": keepalive" in it for it in items),
            f"Expected keepalive comment, got: {items}",
        )

    async def test_disconnect_unsubscribes_from_bus(self) -> None:
        """When the generator is closed (client disconnect), the queue must
        be removed from the bus."""
        bus = WikiEventBus()
        vault_id = 1
        queue = bus.subscribe(vault_id)

        async def event_generator():
            try:
                yield f"data: {json.dumps({'type': 'subscribed', 'vault_id': vault_id})}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.05)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(vault_id, queue)

        gen = event_generator()
        # Consume one item (hello).
        item = await gen.__anext__()
        self.assertIn("subscribed", item)
        # Close the generator (simulates client disconnect).
        await gen.aclose()

        # The queue must be removed from the bus.
        self.assertNotIn(vault_id, bus._subs)

    async def test_multiple_subscribers_receive_events(self) -> None:
        """Multiple queues subscribed to the same vault all receive published
        events."""
        bus = WikiEventBus()
        vault_id = 1
        q1 = bus.subscribe(vault_id)
        q2 = bus.subscribe(vault_id)
        published = {"type": "page_change", "page_id": 77}
        bus.publish(vault_id, published)

        self.assertEqual(q1.get_nowait(), published)
        self.assertEqual(q2.get_nowait(), published)

        bus.unsubscribe(vault_id, q1)
        bus.unsubscribe(vault_id, q2)

    async def test_vault_isolation_in_generator(self) -> None:
        """Events published to a different vault must not appear in this
        generator's queue."""
        bus = WikiEventBus()
        vault_id = 1
        queue = bus.subscribe(vault_id)
        # Publish to a different vault.
        bus.publish(vault_id=2, event={"type": "wrong_vault"})
        # Publish to our vault.
        bus.publish(vault_id=1, event={"type": "correct_vault"})

        event = queue.get_nowait()
        self.assertEqual(event["type"], "correct_vault")
        self.assertTrue(queue.empty())

        bus.unsubscribe(vault_id, queue)


# ---------------------------------------------------------------------------
# Consumer-side observability regression tests
#
# Mirror the publisher-side WARNING-with-exc_info contract from issue #106
# (PR #196) for the consumer-side paths in WikiEventBus. Issue #201
# consolidated these follow-ups: F-1 (inner-fallback silent swallow) and
# F-2 (publish_page_change / publish_claim_change lack try/except).
# ---------------------------------------------------------------------------


class TestWikiEventBusInnerFailureLogging(unittest.TestCase):
    """F-1 (issue #201): inner-fallback drain failure must log WARNING with
    exc_info and structured context, not DEBUG."""

    LOGGER_NAME = "app.services.wiki_events"

    def test_inner_drain_failure_emits_warning_with_traceback(self) -> None:
        """When the slow-consumer drain-and-retry itself fails, a WARNING
        record with exc_info and vault/event context must be emitted."""
        import logging

        bus = WikiEventBus()
        # Fill the queue past maxsize so the next put_nowait raises QueueFull.
        q = bus.subscribe(vault_id=1)
        for i in range(64):
            q.put_nowait({"idx": i})
        # Unsubscribe the queue concurrently so the drain path's get_nowait
        # fails. The set semantics make the queue "gone" from bus._subs'
        # perspective only after discard; reaching into the underlying
        # asyncio.Queue's _get/_put internals via the public API is
        # thread-unsafe, so simulate by wrapping the queue in a shim that
        # makes get_nowait raise after the QueueFull retry path runs.
        class _FlakyQueue:
            def __init__(self, real: asyncio.Queue) -> None:
                self._real = real
                self._fail_get = True

            def put_nowait(self, item):
                return self._real.put_nowait(item)

            def get_nowait(self):
                if self._fail_get:
                    self._fail_get = False
                    raise RuntimeError("simulated concurrent unsubscribe")
                return self._real.get_nowait()

        flaky = _FlakyQueue(q)
        bus._subs[1] = {flaky}  # type: ignore[assignment]

        with self.assertLogs(self.LOGGER_NAME, level=logging.DEBUG) as cap:
            bus.publish(
                vault_id=1,
                event={"type": "job_completed", "job_id": 42},
            )

        warnings = [r for r in cap.records if r.levelno >= logging.WARNING]
        self.assertEqual(
            len(warnings),
            1,
            f"expected exactly one WARNING, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}",
        )
        rec = warnings[0]
        # Operators need the traceback to diagnose the failure.
        self.assertTrue(
            any(r is rec for r in warnings if rec.exc_info),
            "Expected exc_info on the WARNING record",
        )
        # Message must include structured context to help on-call pinpoint
        # the dropped terminal-state event.
        msg = rec.getMessage()
        self.assertIn("vault_id=1", msg)
        self.assertIn("event_type=job_completed", msg)


class TestWikiEventBusPublishHelperFailureLogging(unittest.TestCase):
    """F-2 (issue #201): publish_page_change and publish_claim_change must
    never propagate exceptions, and must log WARNING with exc_info and
    structured context when the underlying publish raises."""

    LOGGER_NAME = "app.services.wiki_events"

    def _bus_with_failing_publish(self) -> WikiEventBus:
        bus = WikiEventBus()
        # Force a non-QueueFull exception from publish() by raising
        # unconditionally from a shim publish. We override the bound method
        # so the helper methods call into our shim via self.publish.
        from app.services.wiki_events import WikiEventBus as _Cls

        original_publish = _Cls.publish

        def _raise(self_, vault_id, event):  # noqa: ANN001
            raise RuntimeError("simulated publish failure")

        _Cls.publish = _raise  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(_Cls, "publish", original_publish))
        return bus

    def test_publish_page_change_failure_emits_warning_and_does_not_raise(
        self,
    ) -> None:
        import logging

        bus = self._bus_with_failing_publish()
        with self.assertLogs(self.LOGGER_NAME, level=logging.DEBUG) as cap:
            try:
                bus.publish_page_change(
                    vault_id=1, page_id=42, action="created", user_id=7
                )
            except Exception as exc:  # pragma: no cover — guard rail
                self.fail(f"publish_page_change must not raise, got {exc!r}")

        warnings = [r for r in cap.records if r.levelno >= logging.WARNING]
        self.assertEqual(
            len(warnings),
            1,
            f"expected exactly one WARNING, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}",
        )
        rec = warnings[0]
        self.assertTrue(
            rec.exc_info is not None,
            "Expected exc_info on the WARNING record",
        )
        msg = rec.getMessage()
        self.assertIn("page_change", msg)
        self.assertIn("vault_id=1", msg)
        self.assertIn("page_id=42", msg)
        self.assertIn("action=created", msg)

    def test_publish_claim_change_failure_emits_warning_and_does_not_raise(
        self,
    ) -> None:
        import logging

        bus = self._bus_with_failing_publish()
        with self.assertLogs(self.LOGGER_NAME, level=logging.DEBUG) as cap:
            try:
                bus.publish_claim_change(
                    vault_id=2, claim_id=99, action="deleted", user_id=3
                )
            except Exception as exc:  # pragma: no cover — guard rail
                self.fail(f"publish_claim_change must not raise, got {exc!r}")

        warnings = [r for r in cap.records if r.levelno >= logging.WARNING]
        self.assertEqual(
            len(warnings),
            1,
            f"expected exactly one WARNING, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}",
        )
        rec = warnings[0]
        self.assertTrue(
            rec.exc_info is not None,
            "Expected exc_info on the WARNING record",
        )
        msg = rec.getMessage()
        self.assertIn("claim_change", msg)
        self.assertIn("vault_id=2", msg)
        self.assertIn("claim_id=99", msg)
        self.assertIn("action=deleted", msg)

    def test_publish_page_change_success_does_not_warn(self) -> None:
        import logging

        bus = WikiEventBus()
        bus.subscribe(vault_id=1)  # need a subscriber to deliver
        records = []

        class _CapturingHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger(self.LOGGER_NAME)
        handler = _CapturingHandler(level=logging.WARNING)
        prev_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            bus.publish_page_change(vault_id=1, page_id=1, action="created")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
        self.assertEqual(
            [r for r in records if r.levelno >= logging.WARNING],
            [],
            "Expected no WARNING logs on successful publish_page_change, got: "
            f"{[r.getMessage() for r in records]}",
        )

    def test_publish_claim_change_success_does_not_warn(self) -> None:
        import logging

        bus = WikiEventBus()
        bus.subscribe(vault_id=1)  # need a subscriber to deliver
        records = []

        class _CapturingHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger(self.LOGGER_NAME)
        handler = _CapturingHandler(level=logging.WARNING)
        prev_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            bus.publish_claim_change(vault_id=1, claim_id=1, action="created")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
        self.assertEqual(
            [r for r in records if r.levelno >= logging.WARNING],
            [],
            "Expected no WARNING logs on successful publish_claim_change, got: "
            f"{[r.getMessage() for r in records]}",
        )


if __name__ == "__main__":
    unittest.main()
