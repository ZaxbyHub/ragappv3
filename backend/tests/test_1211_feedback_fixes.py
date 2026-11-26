"""Feedback-fix regression tests for PR #1211 (not part of the frozen checkpoint)."""

import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
    def test_pending_producer_keeps_pool_open(self):
        import asyncio
        from unittest.mock import MagicMock

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
            pool = MagicMock()
            ok = await chat_routes.shutdown_turn_finalizers(
                [task], pool, 0.01, 0.01
            )
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            return ok, pool.close_all.called

        ok, closed = asyncio.run(run())
        self.assertFalse(ok)
        self.assertFalse(closed)


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


if __name__ == "__main__":
    unittest.main()
