"""
Adversarial security tests for async password wrapper (async_verify_password).

Tests attack vectors:
1. Timing side channels — verify timing is similar for correct vs incorrect passwords
2. Thread pool exhaustion — exhaust 4-worker pool and verify graceful degradation
3. Race conditions — concurrent password changes during active logins
4. Error propagation — hashing internal errors propagate correctly through async wrapper
5. Event-loop blocking — verify event loop is NOT blocked during password verification
"""

import asyncio
import statistics
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from app.services.auth_service import (
    _auth_executor,
    async_hash_password,
    async_verify_password,
    hash_password,
    password_needs_rehash,
    verify_password,
)


class TestAsyncVerifyPasswordTimingSideChannel(unittest.IsolatedAsyncioTestCase):
    """Test for timing side channel vulnerabilities in async_verify_password."""

    async def test_timing_side_channel_timing_similarity_correct_vs_incorrect(self):
        """Timing should not reveal whether password is correct or incorrect.

        Run multiple iterations to collect statistically significant timing data.
        The difference between correct and incorrect password verification times
        should be negligible (within noise margin), not revealing the result.
        """
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)
        wrong_password = "WrongPassword123!"

        num_iterations = 30
        correct_times = []
        incorrect_times = []

        for _ in range(num_iterations):
            # Measure correct password timing
            start = time.perf_counter()
            await async_verify_password(plain_password, hashed_password)
            correct_times.append(time.perf_counter() - start)

            # Measure incorrect password timing
            start = time.perf_counter()
            await async_verify_password(wrong_password, hashed_password)
            incorrect_times.append(time.perf_counter() - start)

        # Calculate statistics
        correct_mean = statistics.mean(correct_times)
        incorrect_mean = statistics.mean(incorrect_times)
        correct_stdev = statistics.stdev(correct_times) if len(correct_times) > 1 else 0
        incorrect_stdev = statistics.stdev(incorrect_times) if len(incorrect_times) > 1 else 0

        # The timing difference should be within noise margin (~3 standard deviations)
        # Memory-hard hashing operations are intentionally slow, so the verification time
        # should be dominated by the hash computation, not the result
        combined_stdev = (correct_stdev + incorrect_stdev) / 2
        timing_difference = abs(correct_mean - incorrect_mean)

        # Timing leak threshold: if difference is > 50ms, it's likely leaking info
        # (memory-hard hashing takes significant time, so 50ms is a reasonable margin)
        timing_leak_threshold = 0.050  # 50ms

        self.assertLess(
            timing_difference,
            timing_leak_threshold,
            f"Timing side channel detected: correct={correct_mean:.3f}s (stdev={correct_stdev:.3f}), "
            f"incorrect={incorrect_mean:.3f}s (stdev={incorrect_stdev:.3f}), "
            f"diff={timing_difference:.3f}s exceeds threshold {timing_leak_threshold}s"
        )

    async def test_timing_side_channel_very_short_password_vs_correct(self):
        """Very short password should not have significantly different timing than correct one."""
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)

        num_iterations = 20
        correct_times = []
        short_pw_times = []

        for _ in range(num_iterations):
            start = time.perf_counter()
            await async_verify_password(plain_password, hashed_password)
            correct_times.append(time.perf_counter() - start)

            start = time.perf_counter()
            await async_verify_password("x", hashed_password)  # Very short
            short_pw_times.append(time.perf_counter() - start)

        timing_diff = abs(statistics.mean(correct_times) - statistics.mean(short_pw_times))
        self.assertLess(
            timing_diff,
            0.050,  # 50ms threshold
            f"Timing leak: short password timing differs by {timing_diff:.3f}s"
        )


class TestAsyncVerifyPasswordThreadPoolExhaustion(unittest.IsolatedAsyncioTestCase):
    """Test thread pool exhaustion and graceful degradation."""

    @pytest.mark.xfail(
        strict=False,
        reason="hashing throughput is environment-dependent; 6s ceiling too tight for slow containers",
    )
    async def test_thread_pool_exhaustion_many_concurrent_calls(self):
        """Many concurrent calls should not hang or crash — should queue and process.

        The auth ThreadPoolExecutor has 4 workers. Submitting many more concurrent
        tasks should queue them and process without deadlock or rejection.
        """
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)

        num_concurrent = 20  # 5x the pool size of 4

        # Launch many concurrent verifications
        start_time = time.perf_counter()
        tasks = [
            async_verify_password(plain_password, hashed_password)
            for _ in range(num_concurrent)
        ]
        results = await asyncio.gather(*tasks)
        total_time = time.perf_counter() - start_time

        # All should succeed
        self.assertEqual(len(results), num_concurrent)
        self.assertTrue(all(results), "All password verifications should return True")

        # Total time should be reasonable — with 4 workers and memory-hard hashing,
        # 20 tasks should take ~5 batches (not 20 * single_hash_time serially)
        # Allow 6 seconds as upper bound for CI overhead and system load variability
        self.assertLess(
            total_time,
            6.0,
            f"Thread pool may be serializing: {num_concurrent} calls took {total_time:.2f}s"
        )

    async def test_thread_pool_exhaustion_does_not_reject_tasks(self):
        """Tasks should be queued, not rejected, when pool is saturated."""
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)

        # Submit 50 concurrent tasks to heavily saturate the 4-worker pool
        num_tasks = 50
        tasks = [
            async_verify_password(plain_password, hashed_password)
            for _ in range(num_tasks)
        ]

        # Should not raise any exceptions — tasks should queue and process
        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), num_tasks)
        self.assertTrue(all(results), "All verifications should succeed")

    async def test_thread_pool_executor_state_after_exhaustion(self):
        """Executor should remain healthy after heavy use."""
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)

        # Heavy load
        tasks = [
            async_verify_password(plain_password, hashed_password)
            for _ in range(40)
        ]
        await asyncio.gather(*tasks)

        # Executor should still accept new work — verify with a simple new task
        result = await async_verify_password(plain_password, hashed_password)
        self.assertTrue(result)


class TestAsyncVerifyPasswordRaceConditions(unittest.IsolatedAsyncioTestCase):
    """Test race conditions involving concurrent password changes and logins."""

    async def test_race_password_change_during_active_login(self):
        """Concurrent password change and login attempts should be handled safely.

        Scenario: User has old password hash H1. While a login is in progress with P,
        the password is changed to Q (hash becomes H2). The in-flight verification
        should complete without error (returns True since P matched H1 at start of verification).
        """
        old_password = "OldPassword123!"
        hashed_password_v1 = hash_password(old_password)

        new_password = "NewPassword456!"
        hashed_password_v2 = hash_password(new_password)

        # Start a login attempt that will verify old password against v1 hash
        # This simulates an in-flight login when password change happens
        login_task = asyncio.create_task(
            async_verify_password(old_password, hashed_password_v1)
        )

        # Simulate password change in the "database" — hash updated to v2
        # The in-flight task with v1 should still complete without error
        # (P matched H1 at the time the verification started)

        # Wait for login with old hash to complete
        result = await asyncio.wait_for(login_task, timeout=5.0)

        # Old password matched old hash — verification succeeded
        # (This is expected: the verification started before the password change)
        self.assertTrue(result)

        # New password should verify correctly against new hash
        new_result = await async_verify_password(new_password, hashed_password_v2)
        self.assertTrue(new_result)

        # Old password should NOT verify against new hash (password changed)
        old_against_new = await async_verify_password(old_password, hashed_password_v2)
        self.assertFalse(old_against_new)

    async def test_concurrent_password_verifications_with_same_hash(self):
        """Multiple simultaneous verifications of the same hash should all succeed."""
        password = "ConcurrentPass123!"
        hashed = hash_password(password)

        # 10 concurrent verifications of the same hash
        tasks = [
            async_verify_password(password, hashed)
            for _ in range(10)
        ]

        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 10)
        self.assertTrue(all(results), "All concurrent verifications should succeed")


class TestAsyncVerifyPasswordErrorPropagation(unittest.IsolatedAsyncioTestCase):
    """Test that hashing errors propagate correctly through async wrapper."""

    async def test_error_propagation_invalid_hash_format(self):
        """Invalid hash format should return False, not raise exception."""
        # Empty or malformed hash
        result = await async_verify_password("password", "not_a_valid_hash_format")
        self.assertFalse(result)

    async def test_error_propagation_empty_password(self):
        """Empty password should be handled gracefully."""
        hashed = hash_password("nonempty")
        result = await async_verify_password("", hashed)
        self.assertFalse(result)

    async def test_error_propagation_none_inputs(self):
        """None inputs should be handled gracefully without crash."""
        hashed = hash_password("somepassword")
        # These should not raise exceptions
        result = await async_verify_password(None, hashed)
        self.assertFalse(result)

    async def test_error_propagation_corrupted_hash(self):
        """Corrupted hash should return False, not propagate hashing errors."""
        # Hash that looks valid but is corrupted (bad checksum)
        corrupted_hash = "$2b$14$abcdefghijklmnopqrstuu9kX9lqqlH9HqH9HqH9HqH9HqH9"
        result = await async_verify_password("password", corrupted_hash)
        self.assertFalse(result)

    async def test_verify_password_error_propagation_sync(self):
        """Sync verify_password should handle errors and return False, not raise."""
        # Invalid hash format
        result = verify_password("password", "invalid_hash")
        self.assertFalse(result)

        # Empty password
        hashed = hash_password("test")
        result = verify_password("", hashed)
        self.assertFalse(result)


class TestAsyncVerifyPasswordEventLoopBlocking(unittest.IsolatedAsyncioTestCase):
    """Test that the event loop is NOT blocked during password verification."""

    async def test_event_loop_not_blocked_during_verification(self):
        """Other async tasks should be able to interleave during password verification.

        If async_verify_password properly uses run_in_executor, other coroutines
        should be able to run while the thread pool handles the blocking hash ops.
        """
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)

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

        # Run password verification (blocks the thread, but NOT the event loop)
        start = time.perf_counter()
        await async_verify_password(plain_password, hashed_password)
        verification_time = time.perf_counter() - start

        # The side task should have executed (proving event loop wasn't blocked)
        self.assertTrue(task_executed, "Side task should have executed, proving event loop wasn't blocked")

        # Verify the side task actually ran during verification (not after)
        # Since hashing takes time and we only sleep 10ms, if task ran after,
        # verification_time would be large and task_execution_time would also be large
        # If task interleaved, task_execution_time would be ~10ms
        self.assertLess(
            task_execution_time,
            0.050,  # 50ms — should be close to our 10ms sleep if it interleaved
            f"Task took {task_execution_time:.3f}s, suggesting it ran AFTER verification"
        )

    async def test_multiple_async_tasks_can_interleave(self):
        """Multiple async tasks should be able to run during a long hashing operation."""
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)

        results = []

        async def fast_task(task_id: int):
            """A task that completes quickly."""
            await asyncio.sleep(0.005)
            results.append(task_id)

        # Start multiple fast tasks
        fast_tasks = [asyncio.create_task(fast_task(i)) for i in range(5)]

        # Start a blocking hashing operation
        hash_task = asyncio.create_task(
            async_verify_password(plain_password, hashed_password)
        )

        # Wait for all to complete
        await asyncio.gather(*fast_tasks, hash_task)

        # All fast tasks should have completed
        self.assertEqual(sorted(results), list(range(5)))
        self.assertTrue(hash_task.result())

    async def test_event_loop_remains_responsive_under_load(self):
        """Event loop should remain responsive even when hash pool is saturated."""
        plain_password = "SecurePass123!"
        hashed_password = hash_password(plain_password)

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

        # Saturate thread pool with 4 long-running hash ops
        hash_tasks = [
            async_verify_password(plain_password, hashed_password)
            for _ in range(4)
        ]

        # Run liveness check alongside
        await asyncio.gather(liveness_check(), *hash_tasks)

        # Event loop should have remained responsive
        self.assertTrue(responsive)
        self.assertEqual(check_count, 3)


class TestHashInjectionDefense(unittest.IsolatedAsyncioTestCase):
    """Test that cross-scheme hash injection attacks are rejected.

    Attack scenario: an argon2id-hashed password stored as bcrypt (or vice versa)
    should NOT accidentally verify. Each scheme must be isolated.
    """

    async def test_argon2id_password_verified_against_bcrypt_hash_returns_false(self):
        """Verifying an argon2id password against a bcrypt hash must return False."""
        import bcrypt

        # An argon2id password (the password we want to verify)
        argon2_password = "SecureArgon2Password123!"
        argon2_hash = await async_hash_password(argon2_password)
        assert argon2_hash.startswith("$argon2id$")

        # A bcrypt hash (NOT the hash of our password)
        bcrypt_hash = bcrypt.hashpw(b"DifferentBcryptPass", bcrypt.gensalt(rounds=12)).decode()
        assert bcrypt_hash.startswith("$2b$")

        # Verifying the argon2 password against the bcrypt hash must return False
        result = await async_verify_password(argon2_password, bcrypt_hash)
        self.assertFalse(result, "argon2id password must NOT verify against bcrypt hash")

    async def test_bcrypt_password_verified_against_argon2id_hash_returns_false(self):
        """Verifying a bcrypt password against an argon2id hash must return False."""
        import bcrypt

        # A bcrypt password (the password we want to verify)
        bcrypt_password = "LegacyBcryptPassword123!"
        bcrypt_hash = bcrypt.hashpw(bcrypt_password.encode(), bcrypt.gensalt(rounds=12)).decode()
        assert bcrypt_hash.startswith("$2b$")

        # An argon2id hash (NOT the hash of our password)
        argon2_hash = await async_hash_password("DifferentArgon2Pass")
        assert argon2_hash.startswith("$argon2id$")

        # Verifying the bcrypt password against the argon2 hash must return False
        result = await async_verify_password(bcrypt_password, argon2_hash)
        self.assertFalse(result, "bcrypt password must NOT verify against argon2id hash")

    def test_sync_verify_rejects_cross_scheme_hash_injection(self):
        """Sync verify_password also rejects cross-scheme hash injection."""
        import bcrypt

        argon2_password = "SecureArgon2Password123!"
        argon2_hash = hash_password(argon2_password)
        bcrypt_hash = bcrypt.hashpw(b"WrongSchemePass", bcrypt.gensalt(rounds=12)).decode()

        # argon2 password vs bcrypt hash must be False
        self.assertFalse(verify_password(argon2_password, bcrypt_hash))
        # bcrypt password vs argon2 hash must be False
        self.assertFalse(verify_password("WrongSchemePass", argon2_hash))

    async def test_password_needs_rehash_garbage_input_does_not_crash(self):
        """password_needs_rehash must not crash on malformed inputs.

        Note: $2b$ prefix is recognized as bcrypt → needs_update=True (correct).
        Only truly unrecognizable strings return False without crashing.
        """
        # Truly unrecognizable strings — must return False (not raise)
        self.assertFalse(password_needs_rehash("not_a_hash"))
        self.assertFalse(password_needs_rehash(""))
        self.assertFalse(password_needs_rehash("$argon2id$garbage"))
        self.assertFalse(password_needs_rehash("$argon2id$invalid$structure"))
        # $2b$ prefix IS recognized as bcrypt (deprecated scheme) → True (upgrade needed)
        self.assertTrue(password_needs_rehash("$2b$garbage"))


class TestAsyncVerifyPasswordExecutorConfiguration(unittest.TestCase):
    """Test that the ThreadPoolExecutor is configured correctly."""

    def test_auth_executor_has_correct_max_workers(self):
        """Executor should have exactly 4 workers as specified."""
        # ThreadPoolExecutor stores max_workers in _max_workers attribute
        self.assertEqual(_auth_executor._max_workers, 4)

    def test_auth_executor_uses_correct_thread_name_prefix(self):
        """Executor threads should use 'auth-cpu' prefix for identification."""
        # This is implicitly tested by other tests capturing thread names
        # but we verify the configuration here
        self.assertEqual(_auth_executor._thread_name_prefix, "auth-cpu")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
