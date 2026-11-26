"""Feedback-fix regression tests for PR #1211 (not part of the frozen checkpoint)."""

import ast
import asyncio
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import lifespan as lifespan_module
from app.api.routes import chat as chat_routes
from app.api.routes import settings as settings_routes
from app.config import Settings, settings

BACKEND = Path(__file__).resolve().parents[1]


class ShutdownInterleavingTest(unittest.TestCase):
    def test_accepted_finalize_before_gate_is_drained(self):
        import asyncio

        original_finalize = chat_routes._finalize_durable_turn_sync
        mock_finalize = MagicMock(return_value=True)
        chat_routes._finalize_durable_turn_sync = mock_finalize
        try:
            result = chat_routes._submit_durable_turn_finalize(
                MagicMock(), {"finalized": False}, "interrupted"
            )
            self.assertTrue(result)
            self.assertTrue(
                asyncio.run(
                    asyncio.to_thread(
                        chat_routes._drain_turn_finalize_executor, 5.0
                    )
                )
            )
            mock_finalize.assert_called_once()
        finally:
            chat_routes._finalize_durable_turn_sync = original_finalize

class ProducerRetirementTest(unittest.TestCase):
    def test_pending_producer_is_returned_and_not_pool_closed(self):
        import asyncio

        release = None

        async def blocked():
            while True:
                try:
                    await release.wait()
                    return
                except asyncio.CancelledError:
                    continue

        async def run():
            nonlocal release
            release = asyncio.Event()
            task = asyncio.create_task(blocked())
            await asyncio.sleep(0)
            pending = await chat_routes._retire_turn_producers([task], 0.01)
            release.set()
            await asyncio.gather(*pending, return_exceptions=True)
            return len(pending)

        self.assertEqual(asyncio.run(run()), 1)


class ShutdownHelperTest(unittest.TestCase):
    def test_pending_producer_blocks_pool_close(self):
        import asyncio

        release = None

        async def blocked():
            while True:
                try:
                    await release.wait()
                    return
                except asyncio.CancelledError:
                    continue

        async def run():
            nonlocal release
            release = asyncio.Event()
            task = asyncio.create_task(blocked())
            await asyncio.sleep(0)
            ok = await chat_routes.shutdown_turn_finalizers([task], 0.01, 0.01)
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            return ok

        self.assertFalse(asyncio.run(run()))

    def test_lifespan_closes_pool_only_after_finalizer_drain(self):
        source = (BACKEND / "app" / "lifespan.py").read_text(encoding="utf-8")
        helper_at = source.find("await shutdown_turn_finalizers(")
        close_at = source.find("app.state.db_pool.close_all()")
        self.assertGreater(helper_at, 0)
        self.assertGreater(close_at, 0)
        self.assertLess(helper_at, close_at)


class SettingsContractTest(unittest.TestCase):
    def test_assistant_cap_validator_rejects_zero(self):
        with self.assertRaises(ValueError):
            Settings.validate_assistant_message_max_chars(0)

    def test_assistant_cap_is_on_public_settings_wire(self):
        self.assertIn("assistant_message_max_chars", settings_routes.ALLOWED_FIELDS)
        self.assertIn(
            "assistant_message_max_chars", settings_routes.SettingsResponse.model_fields
        )
        self.assertEqual(
            settings_routes._build_settings_dict()["assistant_message_max_chars"],
            settings.assistant_message_max_chars,
        )


class ShutdownCloseContractTest(unittest.TestCase):
    def _run_helper(self, drained: bool):
        async def fake_shutdown(*_args, **_kwargs):
            return drained

        app = SimpleNamespace(
            state=SimpleNamespace(db_pool=MagicMock(name="db_pool"))
        )
        with (
            patch.object(chat_routes, "shutdown_turn_finalizers", fake_shutdown),
            patch.object(chat_routes, "_turn_registry", lambda: {}),
        ):
            asyncio.run(lifespan_module._finalize_turns_and_close_pool(app))
        return app.state.db_pool

    def test_successful_drain_closes_pool_exactly_once(self):
        pool = self._run_helper(True)
        pool.close_all.assert_called_once_with()

    def test_denied_drain_does_not_close_pool(self):
        pool = self._run_helper(False)
        pool.close_all.assert_not_called()

    def test_total_budget_covers_executor_queue_time(self):
        async def no_producers(*_args, **_kwargs):
            return set()

        def slow_drain(_timeout=None):
            time.sleep(0.2)
            return True

        async def run():
            started = time.monotonic()
            result = await chat_routes.shutdown_turn_finalizers([], 0.01, 0.01)
            return result, time.monotonic() - started

        with (
            patch.object(chat_routes, "_retire_turn_producers", no_producers),
            patch.object(chat_routes, "_drain_turn_finalize_executor", slow_drain),
        ):
            result, elapsed = asyncio.run(run())
        self.assertFalse(result)
        self.assertLess(elapsed, 0.15)


class EagerFinalizeTrackingTest(unittest.TestCase):
    def test_eager_finalize_is_in_the_drained_worker_set(self):
        started = threading.Event()
        release = threading.Event()
        turn_state = {
            "payload": {"session_id": "s", "turn_id": "t"},
            "collected_content": [],
            "finalized": False,
        }

        def blocked(_pool, _state, _status):
            started.set()
            release.wait(timeout=3.0)
            return True

        with patch.object(chat_routes, "_finalize_durable_turn_sync", blocked):
            future = chat_routes._submit_durable_turn_finalize_future(
                MagicMock(), turn_state, "completed"
            )
            self.assertIsNotNone(future)
            with chat_routes._TURN_FINALIZE_FUTURES_LOCK:
                self.assertIn(future, chat_routes._TURN_FINALIZE_FUTURES)
            self.assertTrue(started.wait(timeout=2.0))
            waiter = threading.Thread(
                target=lambda: chat_routes._wait_durable_turn_finalize(future)
            )
            waiter.start()
            try:
                with chat_routes._TURN_FINALIZE_FUTURES_LOCK:
                    self.assertIn(
                        future,
                        chat_routes._TURN_FINALIZE_FUTURES,
                        "drain lost the tracked eager finalize",
                    )
            finally:
                release.set()
                waiter.join(timeout=3.0)
        self.assertFalse(waiter.is_alive())


if __name__ == "__main__":
    unittest.main()
