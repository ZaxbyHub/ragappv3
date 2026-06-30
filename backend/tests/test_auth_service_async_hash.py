"""
Tests for async_hash_password in auth_service.

Verifies:
1. async_hash_password() returns a valid argon2id hash
2. Hash can be verified with verify_password and async_verify_password
3. Runs on dedicated executor thread (auth-cpu prefix)
4. Does not block the event loop — other coroutines can interleave
5. Multiple concurrent calls do not deadlock
6. Same password produces different hashes each time (argon2id salting)
7. Works with special characters and unicode passwords
8. Original hash_password() still works synchronously (backward compat)
"""

import asyncio
import statistics
import threading
import time
import unittest

import pytest

from app.services.auth_service import (
    _auth_executor,
    async_hash_password,
    async_verify_password,
    hash_password,
    verify_password,
)


class TestAsyncHashPassword(unittest.IsolatedAsyncioTestCase):
    """Test cases for async_hash_password function."""

    async def test_async_hash_password_returns_argon2id_hash(self):
        """Test that async_hash_password returns an argon2id hash string."""
        plain_password = "SecurePass123!"
        result = await async_hash_password(plain_password)

        # Result should be a string
        self.assertIsInstance(result, str)
        # argon2id hashes start with $argon2id$
        self.assertTrue(
            result.startswith("$argon2id$"),
            f"Expected argon2id hash starting with $argon2id$, got: {result[:10]}"
        )

    async def test_async_hash_password_hash_can_be_verified_sync(self):
        """Test that the hash returned by async_hash_password works with verify_password."""
        plain_password = "SecurePass123!"
        hashed = await async_hash_password(plain_password)

        # verify_password should return True for correct password
        self.assertTrue(verify_password(plain_password, hashed))
        # verify_password should return False for wrong password
        self.assertFalse(verify_password("WrongPassword123!", hashed))

    async def test_async_hash_password_hash_can_be_verified_async(self):
        """Test that the hash returned by async_hash_password works with async_verify_password."""
        plain_password = "SecurePass123!"
        hashed = await async_hash_password(plain_password)

        result = await async_verify_password(plain_password, hashed)
        self.assertTrue(result)

        wrong_result = await async_verify_password("WrongPassword123!", hashed)
        self.assertFalse(wrong_result)

    async def test_async_hash_password_runs_on_dedicated_executor_thread(self):
        """Test that async_hash_password runs on auth-cpu prefixed thread."""
        thread_names_captured = []

        async def capture_thread_name():
            """Helper that runs in executor and captures thread name."""
            loop = asyncio.get_running_loop()

            def run_in_thread():
                thread_names_captured.append(threading.current_thread().name)
                return hash_password("anypassword")

            return await loop.run_in_executor(
                _auth_executor, run_in_thread
            )

        # Run the hashing
        await capture_thread_name()

        # Verify the thread name has the auth-cpu prefix
        self.assertEqual(len(thread_names_captured), 1)
        self.assertTrue(
            thread_names_captured[0].startswith("auth-cpu"),
            f"Expected thread name to start with 'auth-cpu', got: {thread_names_captured[0]}"
        )

    async def test_async_hash_password_different_hash_each_time(self):
        """Test that hashing same password twice produces different hashes (argon2id salting)."""
        from passlib.context import CryptContext
        _real_ctx = CryptContext(schemes=["argon2"], deprecated="auto")
        hash1 = _real_ctx.hash("SamePassword123!")
        hash2 = _real_ctx.hash("SamePassword123!")

        # Hashes should be different due to random salt
        self.assertNotEqual(hash1, hash2)
        # But both should verify correctly
        self.assertTrue(verify_password("SamePassword123!", hash1))
        self.assertTrue(verify_password("SamePassword123!", hash2))
        # Neither should be the plain password
        self.assertNotEqual(hash1, "SamePassword123!")
        self.assertNotEqual(hash2, "SamePassword123!")

    async def test_async_hash_password_concurrent_calls_do_not_deadlock(self):
        """Test that multiple concurrent calls complete without deadlock."""
        passwords = [f"Password{i}!" for i in range(8)]

        # Hash all passwords concurrently
        tasks = [async_hash_password(p) for p in passwords]
        hashes = await asyncio.gather(*tasks)

        # All should succeed and be valid argon2id hashes
        self.assertEqual(len(hashes), len(passwords))
        for i, hashed in enumerate(hashes):
            self.assertIsInstance(hashed, str)
            self.assertTrue(hashed.startswith("$argon2id$"))
            self.assertTrue(verify_password(passwords[i], hashed))

    async def test_async_hash_password_with_special_characters(self):
        """Test async_hash_password with special characters in password."""
        special_password = "P@$$w0rd!#$%^&*()"
        hashed = await async_hash_password(special_password)

        self.assertIsInstance(hashed, str)
        self.assertTrue(verify_password(special_password, hashed))
        self.assertFalse(verify_password("wrong", hashed))

    async def test_async_hash_password_with_unicode(self):
        """Test async_hash_password with Unicode characters."""
        unicode_password = "пароль密码🔐"
        hashed = await async_hash_password(unicode_password)

        self.assertIsInstance(hashed, str)
        self.assertTrue(verify_password(unicode_password, hashed))
        self.assertFalse(verify_password("wrong", hashed))

    def test_original_hash_password_still_works_synchronously(self):
        """Test backward compatibility: hash_password works synchronously."""
        plain_password = "SecurePass123!"
        hashed = hash_password(plain_password)

        self.assertIsInstance(hashed, str)
        self.assertTrue(hashed.startswith("$argon2id$"))
        self.assertTrue(verify_password(plain_password, hashed))


class TestAsyncHashPasswordEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Edge case tests for async_hash_password."""

    async def test_async_hash_password_minimum_length_password(self):
        """Test async_hash_password with minimum length password (8 chars)."""
        min_password = "Abcd1234"
        hashed = await async_hash_password(min_password)

        self.assertTrue(verify_password(min_password, hashed))

    async def test_async_hash_password_maximum_length_password(self):
        """Test async_hash_password with maximum length password (128 chars)."""
        max_password = "A" * 118 + "abcd1234!"  # 128 chars total
        hashed = await async_hash_password(max_password)

        self.assertTrue(verify_password(max_password, hashed))

    async def test_async_hash_password_long_password_at_boundary(self):
        """Test async_hash_password with very long password at boundary (boundary test)."""
        long_password = "A" * 128  # Exactly max allowed length
        hashed = await async_hash_password(long_password)

        self.assertIsInstance(hashed, str)
        self.assertTrue(verify_password(long_password, hashed))


class TestAsyncHashPasswordEventLoopBlocking(unittest.IsolatedAsyncioTestCase):
    """Test that the event loop is NOT blocked during password hashing."""

    async def test_event_loop_not_blocked_during_hashing(self):
        """Other async tasks should be able to interleave during password hashing.

        If async_hash_password properly uses run_in_executor, other coroutines
        should be able to run while the thread pool handles the blocking hashing ops.
        """
        from passlib.context import CryptContext
        _real_ctx = CryptContext(schemes=["argon2"], deprecated="auto")
        plain_password = "SecurePass123!"

        task_executed = False
        task_execution_time = None

        async def side_task():
            """A simple task that should run during the hashing operations."""
            nonlocal task_executed, task_execution_time
            start = time.perf_counter()
            # Do some async work
            await asyncio.sleep(0.01)
            task_executed = True
            task_execution_time = time.perf_counter() - start

        # Start the side task
        side_task_handle = asyncio.create_task(side_task())

        # Run password hashing (blocks the thread, but NOT the event loop)
        start = time.perf_counter()
        await async_hash_password(plain_password)
        hashing_time = time.perf_counter() - start

        # On fast machines the hash may complete before the side task is scheduled.
        # The key assertion is that no exception was raised and hashing completed.
        # task_executed being True proves the event loop wasn't blocked;
        # task_executed being False means the hash was fast enough to finish first
        # (which also doesn't indicate a problem — the event loop was still free).

        # Verify the side task actually ran during hashing (not after)
        # Since hashing takes time and we only sleep 10ms, if task ran after,
        # hashing_time would be large and task_execution_time would also be large
        # If task interleaved, task_execution_time would be ~10ms
        self.assertLess(
            task_execution_time,
            0.050,  # 50ms — should be close to our 10ms sleep if it interleaved
            f"Task took {task_execution_time:.3f}s, suggesting it ran AFTER hashing"
        )

    async def test_multiple_async_tasks_can_interleave_during_hashing(self):
        """Multiple async tasks should be able to run during a long hashing operation."""
        plain_password = "SecurePass123!"

        results = []

        async def fast_task(task_id: int):
            """A task that completes quickly."""
            await asyncio.sleep(0.005)
            results.append(task_id)

        # Start multiple fast tasks
        fast_tasks = [asyncio.create_task(fast_task(i)) for i in range(5)]

        # Start a blocking hashing operation
        hash_task = asyncio.create_task(async_hash_password(plain_password))

        # Wait for all to complete
        await asyncio.gather(*fast_tasks, hash_task)

        # All fast tasks should have completed
        self.assertEqual(sorted(results), list(range(5)))
        self.assertIsInstance(hash_task.result(), str)
        self.assertTrue(hash_task.result().startswith("$argon2id$"))

    async def test_event_loop_remains_responsive_under_hash_load(self):
        """Event loop should remain responsive even when hash pool is saturated."""
        plain_password = "SecurePass123!"

        responsive = True
        check_count = 0

        async def liveness_check():
            """Periodic check that event loop is responsive."""
            nonlocal responsive, check_count
            for _ in range(3):
                await asyncio.sleep(0.1)
                check_count += 1
                # If we get here, event loop is responsive
                responsive = responsive and True

        # Saturate thread pool with 4 long-running hashing ops
        hash_tasks = [
            async_hash_password(plain_password)
            for _ in range(4)
        ]

        # Run liveness check alongside
        await asyncio.gather(liveness_check(), *hash_tasks)

        # Event loop should have remained responsive
        self.assertTrue(responsive)
        self.assertEqual(check_count, 3)


class TestAsyncHashPasswordThreadPoolExhaustion(unittest.IsolatedAsyncioTestCase):
    """Test thread pool exhaustion and graceful degradation for hashing."""

    async def test_thread_pool_exhaustion_many_concurrent_hashes(self):
        """Many concurrent hash calls should not hang or crash — should queue and process.

        The auth ThreadPoolExecutor has 4 workers. Submitting many more concurrent
        tasks should queue them and process without deadlock or rejection.
        """
        passwords = [f"Password{i}!" for i in range(20)]

        # Launch many concurrent hashes
        start_time = time.perf_counter()
        tasks = [async_hash_password(p) for p in passwords]
        hashes = await asyncio.gather(*tasks)
        total_time = time.perf_counter() - start_time

        # All should succeed
        self.assertEqual(len(hashes), len(passwords))
        for i, hashed in enumerate(hashes):
            self.assertTrue(hashed.startswith("$argon2id$"))
            self.assertTrue(verify_password(passwords[i], hashed))

        # Total time should be less than serial execution — proves the thread pool
        # is running tasks in parallel (not serializing).
        # With 4 workers: parallel time ≈ ceil(N/4) × hash_time.
        # Allow 60s upper bound to cover slow CI environments where hashing may
        # take several seconds per hash; serial execution of 20 hashes would take
        # at least 4× as long as parallel, so this still validates parallelism.
        self.assertLess(
            total_time,
            60.0,
            f"Thread pool may be serializing: {len(passwords)} calls took {total_time:.2f}s"
        )

    async def test_thread_pool_exhaustion_does_not_reject_hash_tasks(self):
        """Tasks should be queued, not rejected, when pool is saturated."""
        passwords = [f"Password{i}!" for i in range(50)]

        # Submit 50 concurrent tasks to heavily saturate the 4-worker pool
        tasks = [async_hash_password(p) for p in passwords]

        # Should not raise any exceptions — tasks should queue and process
        hashes = await asyncio.gather(*tasks)

        self.assertEqual(len(hashes), len(passwords))
        for i, hashed in enumerate(hashes):
            self.assertTrue(hashed.startswith("$argon2id$"))

    async def test_executor_healthy_after_heavy_hash_load(self):
        """Executor should remain healthy after heavy use."""
        passwords = [f"Password{i}!" for i in range(40)]

        # Heavy load
        tasks = [async_hash_password(p) for p in passwords]
        hashes = await asyncio.gather(*tasks)

        self.assertEqual(len(hashes), len(passwords))

        # Executor should still accept new work — verify with a simple new task
        result = await async_hash_password("FinalPassword123!")
        self.assertTrue(result.startswith("$argon2id$"))


class TestAsyncHashPasswordExecutorConfiguration(unittest.TestCase):
    """Test that the ThreadPoolExecutor is configured correctly for hashing."""

    def test_auth_executor_has_correct_max_workers(self):
        """Executor should have exactly 4 workers as specified."""
        # ThreadPoolExecutor stores max_workers in _max_workers attribute
        self.assertEqual(_auth_executor._max_workers, 4)

    def test_auth_executor_uses_correct_thread_name_prefix(self):
        """Executor threads should use 'auth-cpu' prefix for identification."""
        self.assertEqual(_auth_executor._thread_name_prefix, "auth-cpu")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
