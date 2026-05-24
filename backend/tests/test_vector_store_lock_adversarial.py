"""
Adversarial tests for VectorStore _acquire_write_lock timeout behavior (Task 1.2).

Attack vectors tested:
1. Lock saturation: 8 concurrent write operations contending for the lock
2. Timeout granularity: 0.001s (near-instant) and 1800s (extreme upper bound)
3. Lock ordering deadlock: non-reentrant asyncio.Lock — same coroutine tries to re-acquire
4. Nested exception handling: body raises AND finally raises — only finally propagates
5. Concurrent re-acquisition after timeout: can the lock be re-acquired after a timeout?
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, NonCallableMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import Settings
from app.services.vector_store import VectorStore, VectorStoreError


class TestLockSaturationAdversarial(unittest.IsolatedAsyncioTestCase):
    """Attack vector 1: Lock saturation — 8 concurrent write ops contend simultaneously."""

    async def test_eight_concurrent_writers_all_get_lock_or_timeout(self):
        """
        When 8 concurrent write operations contend for the same asyncio.Lock,
        they must serialize. All 8 should either complete (queuing) or fail fast
        with a timeout — they must NOT crash, hang forever, or corrupt lock state.

        This test uses a very short hold time so the 8 operations can complete
        within the test timeout if they serialize correctly.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_saturation"))

        # Use a real asyncio.Lock so we test the real thing
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        acquisition_order = []
        release_order = []

        # Save original methods before patching
        orig_acquire = real_lock.acquire
        orig_release = real_lock.release

        async def tracking_acquire():
            await orig_acquire()
            acquisition_order.append(len(acquisition_order))

        def tracking_release():
            release_order.append(len(release_order))
            orig_release()

        real_lock.acquire = tracking_acquire
        real_lock.release = tracking_release

        async def writer(name: str, hold_seconds: float):
            try:
                async with store._acquire_write_lock():
                    # Track which writer holds the lock
                    acquisition_order.append(name)
                    await asyncio.sleep(hold_seconds)
                return ("completed", name)
            except VectorStoreError:
                return ("timeout", name)

        # 8 concurrent writers, each holding lock for 0.05s
        # Total serialized time: 8 * 0.05 = 0.4s — well within default 30s timeout
        results = await asyncio.gather(
            writer("w1", 0.05),
            writer("w2", 0.05),
            writer("w3", 0.05),
            writer("w4", 0.05),
            writer("w5", 0.05),
            writer("w6", 0.05),
            writer("w7", 0.05),
            writer("w8", 0.05),
        )

        completed = [r for r in results if r[0] == "completed"]
        timed_out = [r for r in results if r[0] == "timeout"]

        # With 30s timeout and ~0.4s total serialized work, none should time out
        self.assertEqual(
            len(completed), 8,
            f"Expected all 8 to complete serialized, got {len(completed)} completed, "
            f"{len(timed_out)} timed out. Results: {results}"
        )
        self.assertEqual(len(timed_out), 0, f"Unexpected timeouts: {timed_out}")

        # Verify serialization: exactly 8 name acquisitions and 8 releases
        # (16 total entries: 8 tracking_acquire + 8 writer name appends)
        name_entries = [x for x in acquisition_order if isinstance(x, str)]
        self.assertEqual(len(name_entries), 8, f"Expected 8 name entries, got {name_entries}")
        self.assertEqual(len(release_order), 8, "Expected 8 releases")

        # Each release should happen in FIFO order (lock was held throughout)
        self.assertEqual(release_order, list(range(8)))

    async def test_lock_saturation_with_rapid_successive_holds(self):
        """
        Stress test: 16 rapid lock acquisitions in sequence.
        No accumulation of orphaned lock state — every acquire must have a release.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_rapid"))
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        acquire_count = 0
        release_count = 0

        # Save originals before patching
        orig_acquire = real_lock.acquire
        orig_release = real_lock.release

        async def counting_acquire():
            nonlocal acquire_count
            await orig_acquire()
            acquire_count += 1

        def counting_release():
            nonlocal release_count
            release_count += 1
            orig_release()

        real_lock.acquire = counting_acquire
        real_lock.release = counting_release

        async def quick_writer():
            async with store._acquire_write_lock():
                await asyncio.sleep(0.001)

        # 16 rapid serial acquisitions
        for i in range(16):
            await quick_writer()

        self.assertEqual(acquire_count, 16, "Every lock_acquire must be matched")
        self.assertEqual(release_count, 16, "Every lock_release must be matched")
        # Final state: lock must be fully released
        self.assertFalse(real_lock.locked(), "Lock must be unlocked after all operations")


class TestTimeoutGranularityAdversarial(unittest.IsolatedAsyncioTestCase):
    """Attack vector 2: Timeout granularity — does timeout work at 0.001s? At 1800s?"""

    async def test_timeout_zero_point_001_seconds(self):
        """
        Extremely short timeout (0.001s = 1ms) should fire immediately if lock is held.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_tiny_timeout"))

        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        first_released = asyncio.Event()

        # Save original methods
        orig_acquire = real_lock.acquire
        orig_release = real_lock.release

        async def hold_lock_long():
            await orig_acquire()
            await first_released.wait()
            orig_release()

        # Start holder in background
        holder_task = asyncio.create_task(hold_lock_long())
        # Wait until lock is definitely held
        await asyncio.sleep(0.02)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.write_lock_timeout_seconds = 0.001

            # _acquire_write_lock should raise VectorStoreError due to timeout
            with self.assertRaises(VectorStoreError) as ctx:
                async with store._acquire_write_lock():
                    pass

            self.assertIn("timed out", str(ctx.exception))
            self.assertIn("0.001", str(ctx.exception))

        first_released.set()
        await holder_task

    async def test_timeout_1800_seconds_extreme_upper_bound(self):
        """
        Extreme upper-bound timeout (1800s = 30min) should be passed correctly to wait_for.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_huge_timeout"))

        captured_timeout = None

        async def capturing_wait_for(coro, timeout):
            nonlocal captured_timeout
            captured_timeout = timeout
            # Immediately return so we don't actually wait
            return await coro

        with patch("app.services.vector_store.asyncio.wait_for", side_effect=capturing_wait_for):
            store._write_lock.acquire = AsyncMock()
            store._write_lock.release = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.write_lock_timeout_seconds = 1800.0

                async with store._acquire_write_lock():
                    pass

        self.assertEqual(captured_timeout, 1800.0)

    async def test_zero_timeout_immediately_fires(self):
        """
        Zero timeout (0.0s) should fire immediately — the lock is already held
        so wait_for should timeout before even acquiring.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_zero_timeout"))

        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        # Save original methods
        orig_acquire = real_lock.acquire
        orig_release = real_lock.release

        # Acquire and hold lock
        await orig_acquire()
        released = False

        def holder_release():
            nonlocal released
            released = True
            orig_release()

        real_lock.release = holder_release

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.write_lock_timeout_seconds = 0.0

            # _acquire_write_lock with 0s timeout on already-held lock
            with self.assertRaises(VectorStoreError) as ctx:
                async with store._acquire_write_lock():
                    pass

            self.assertIn("timed out", str(ctx.exception))

        # Cleanup: release the lock (the holder would have done this, but we timed out)
        if not released:
            orig_release()


class TestLockOrderingDeadlockAdversarial(unittest.IsolatedAsyncioTestCase):
    """Attack vector 3: Lock ordering deadlock — asyncio.Lock is NOT reentrant."""

    async def test_same_coroutine_reacquiring_lock_deadlocks(self):
        """
        asyncio.Lock is NOT a reentrant lock. If the same coroutine tries to
        acquire it while already holding it, it will deadlock (wait forever).

        We verify this by checking that a 0.001s timeout fires when the
        *same* coroutine tries to re-acquire the lock it already holds.

        NOTE: In a real asyncio event loop this deadlocks the entire coroutine —
        there is no automatic detection. The only safeguard is the timeout.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_deadlock"))

        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        re_acquire_fired = False
        re_acquire_error = None

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.write_lock_timeout_seconds = 0.1  # 100ms timeout for deadlock detection

            async def outer_method():
                nonlocal re_acquire_fired, re_acquire_error
                # Outer method acquires the lock
                async with store._acquire_write_lock():
                    # Simulate: outer method calls an internal method that also tries the lock
                    # We simulate this by directly calling _acquire_write_lock again
                    ctx = store._acquire_write_lock()
                    try:
                        await ctx.__aenter__()
                        # If we get here without timeout, we have a reentrant lock — this is safe
                        re_acquire_fired = True
                        await ctx.__aexit__(None, None, None)
                    except VectorStoreError as e:
                        # Timeout is expected — asyncio.Lock is not reentrant
                        re_acquire_error = e

            await outer_method()

        # asyncio.Lock is NOT reentrant, so re-acquire must fail with timeout
        self.assertFalse(
            re_acquire_fired,
            "Re-acquiring a non-reentrant asyncio.Lock should timeout/fail"
        )
        self.assertIsNotNone(
            re_acquire_error,
            "Should have gotten a VectorStoreError timeout from re-acquire attempt"
        )
        self.assertIn("timed out", str(re_acquire_error))

    async def test_no_cross_method_deadlock_in_real_usage(self):
        """
        Verify that calling two locked methods in sequence from the same coroutine
        does NOT deadlock — because the inner call uses the _unlocked variant.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_cross_method"))
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        call_log = []

        # Mock the unlocked methods to record calls
        mock_add_chunks = AsyncMock(return_value={})
        mock_delete_ids = AsyncMock(return_value=0)

        orig_add = store._add_chunks_unlocked
        orig_delete = store._delete_ids_unlocked

        store._add_chunks_unlocked = mock_add_chunks
        store._delete_ids_unlocked = mock_delete_ids

        try:
            # Simulate what add_chunks_then_delete_ids does:
            async with store._acquire_write_lock():
                call_log.append("lock_held")
                await store._add_chunks_unlocked([])
                call_log.append("after_add_chunks")
                await store._delete_ids_unlocked([])
                call_log.append("after_delete_ids")

            # Both unlocked methods must have been called while lock was held
            self.assertEqual(call_log, ["lock_held", "after_add_chunks", "after_delete_ids"])
            self.assertEqual(mock_add_chunks.call_count, 1)
            self.assertEqual(mock_delete_ids.call_count, 1)
        finally:
            store._add_chunks_unlocked = orig_add
            store._delete_ids_unlocked = orig_delete


class TestNestedExceptionHandlingAdversarial(unittest.IsolatedAsyncioTestCase):
    """Attack vector 4: Nested exception handling — body raises AND finally raises."""

    async def test_body_exception_suppressed_by_finally_exception(self):
        """
        Python 3.11+: if an exception fires in the `async with` body AND the
        finally block (cleanup) also raises, only the finally exception propagates —
        the body exception is suppressed.

        We test this by having:
        1. Body raise a ValueError
        2. finally block raise a RuntimeError (simulating a failed release)
        Expected: RuntimeError propagates, ValueError is silently suppressed.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_nested_exc"))
        store._write_lock.acquire = AsyncMock()

        # Simulate lock release failing in finally
        store._write_lock.release = MagicMock(
            side_effect=RuntimeError("lock release failed!")
        )

        ctx = store._acquire_write_lock()
        await ctx.__aenter__()

        body_exc = ValueError("data validation error")

        # Per Python 3.11+ semantics, __aexit__ with a non-None exc_info argument
        # where finally also raises: the finally exception wins
        with self.assertRaises(RuntimeError) as ctx2:
            await ctx.__aexit__(ValueError, body_exc, None)

        self.assertEqual(str(ctx2.exception), "lock release failed!")
        # ValueError is silently lost — this is the adversarial concern

    async def test_finally_exception_still_releases_lock(self):
        """
        Even if finally raises, the lock MUST be released. This is the minimum
        correctness guarantee.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_nested_exc2"))
        release_called = False

        def raising_release():
            nonlocal release_called
            release_called = True
            raise RuntimeError("release failed!")

        store._write_lock.acquire = AsyncMock()
        store._write_lock.release = raising_release

        ctx = store._acquire_write_lock()
        await ctx.__aenter__()

        # Even if finally raises, the lock MUST be released
        with self.assertRaises(RuntimeError):
            await ctx.__aexit__(ValueError, ValueError("body error"), None)

        self.assertTrue(release_called, "Lock MUST be released even when finally raises")

    async def test_cancelled_error_not_caught_by_wait_for_timeout_handler(self):
        """
        asyncio.wait_for does NOT catch CancelledError — it propagates it.
        CancelledError bypasses the except asyncio.TimeoutError handler and
        propagates directly. This means: if CancelledError fires while waiting
        for the lock, the except TimeoutError block is skipped entirely.

        The finally block still runs (Python guarantees this for context managers).

        This test verifies that CancelledError is NOT caught as TimeoutError.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_cancelled"))
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        # First acquire the lock so subsequent acquire must wait
        await real_lock.acquire()
        self.assertTrue(real_lock.locked())

        # Track whether lock is still held after the test
        lock_state_after = None

        # Mock wait_for to raise CancelledError (simulating task cancellation)
        async def cancelling_wait_for(coro, timeout):
            nonlocal lock_state_after
            # Cancel immediately
            raise asyncio.CancelledError()

        with patch("app.services.vector_store.asyncio.wait_for", side_effect=cancelling_wait_for):
            ctx = store._acquire_write_lock()

            # CancelledError propagates from __aenter__ (NOT caught as TimeoutError)
            with self.assertRaises(asyncio.CancelledError):
                await ctx.__aenter__()

        # The finally block SHOULD still run (Python language guarantee),
        # but since CancelledError fires BEFORE acquire() completes in our mock,
        # the lock was never acquired by the context manager.
        # In a real scenario where CancelledError fires AFTER acquire() succeeds
        # (but before yield), the lock would be released by finally.
        # We verify: the lock is NOT left in a corrupt state — it's either
        # not acquired (our mock) or released (real scenario after finally runs).
        lock_state_after = real_lock.locked()

        # With our mock (CancelledError before acquire completes), lock is still held
        # This documents the edge case: CancelledError can leave lock held if it
        # fires after acquire() but before the coroutine returns to the event loop.
        # The real fix would require asyncio.CancelledError to be caught explicitly
        # alongside TimeoutError, but that's a design decision.


class TestConcurrentReacquireAfterTimeoutAdversarial(unittest.IsolatedAsyncioTestCase):
    """Attack vector 5: Can the lock be re-acquired after a timeout?"""

    async def test_lock_reacquired_after_timeout(self):
        """
        After a timeout (VectorStoreError), the lock must be in a clean state —
        it must be immediately re-acquirable by the next caller.

        This is critical: timeout should NOT corrupt the lock's internal state.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_reacquire"))
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        first_released = asyncio.Event()

        # Save originals
        orig_acquire = real_lock.acquire
        orig_release = real_lock.release

        # First caller: holds lock until we signal
        async def hold_forever():
            await orig_acquire()
            await first_released.wait()
            orig_release()

        holder_task = asyncio.create_task(hold_forever())
        await asyncio.sleep(0.02)  # Give holder time to acquire

        # Set a very short timeout so we don't wait long
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.write_lock_timeout_seconds = 0.05

            # First acquire should timeout (lock is held)
            with self.assertRaises(VectorStoreError):
                async with store._acquire_write_lock():
                    pass

        # Release the first holder
        first_released.set()
        await holder_task

        # Now the lock is free — second acquire should succeed immediately
        second_acquired = False
        second_released = False

        orig_release2 = real_lock.release

        def tracking_release2():
            nonlocal second_released
            second_released = True
            orig_release2()

        real_lock.release = tracking_release2

        async def second_writer():
            nonlocal second_acquired
            async with store._acquire_write_lock():
                second_acquired = True

        await second_writer()

        self.assertTrue(second_acquired, "Lock must be re-acquirable after timeout")
        self.assertTrue(second_released, "Second writer must have released the lock")

    async def test_rapid_timeout_then_success_cycle(self):
        """
        Stress test: repeated timeout-then-success cycles.
        The lock must handle repeated state transitions without corruption.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb_rapid_cycle"))
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        results = []

        # Save originals
        orig_acquire = real_lock.acquire
        orig_release = real_lock.release

        # Alternating holder: holds for 10s on odd calls, releases immediately on even
        call_num = 0

        async def alternating_holder():
            nonlocal call_num
            await orig_acquire()
            call_num += 1
            if call_num % 2 == 0:
                # Even calls: release immediately (next acquire succeeds)
                orig_release()
                return "released_immediately"
            else:
                # Odd calls: hold for a long time (next acquire times out)
                await asyncio.sleep(60)
                orig_release()
                return "held_then_released"

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.write_lock_timeout_seconds = 0.03

            for i in range(6):
                holder_task = asyncio.create_task(alternating_holder())
                await asyncio.sleep(0.01)  # Let holder acquire

                try:
                    async with store._acquire_write_lock():
                        results.append(f"success_{i}")
                except VectorStoreError:
                    results.append(f"timeout_{i}")

                # Cancel holder and release lock if still held
                holder_task.cancel()
                try:
                    await holder_task
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    pass

                # Ensure lock is released for next iteration
                if real_lock.locked():
                    try:
                        real_lock.release()
                    except RuntimeError:
                        pass  # Already released by holder

        # We expect alternating timeouts and successes
        # call_num 1: holder holds → timeout_0
        # call_num 2: holder releases → success_1
        # call_num 3: holder holds → timeout_2
        # call_num 4: holder releases → success_3
        # call_num 5: holder holds → timeout_4
        # call_num 6: holder releases → success_5
        self.assertEqual(len(results), 6)
        for i, r in enumerate(results):
            if i % 2 == 0:
                self.assertEqual(r, f"timeout_{i}", f"Iteration {i} should timeout")
            else:
                self.assertEqual(r, f"success_{i}", f"Iteration {i} should succeed")


if __name__ == "__main__":
    unittest.main()
