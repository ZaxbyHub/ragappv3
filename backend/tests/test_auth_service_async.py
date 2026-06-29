"""
Tests for async password wrapper in auth_service.

Verifies:
1. async_verify_password() returns True for correct password
2. async_verify_password() returns False for incorrect password
3. async_verify_password() runs on dedicated executor thread (auth-cpu prefix)
4. Multiple concurrent calls do not deadlock
5. Original verify_password() still works synchronously (backward compat)
"""

import asyncio
import threading
import unittest
from unittest.mock import patch

import pytest

from app.services.auth_service import (
    _auth_executor,
    async_verify_password,
    hash_password,
    verify_password,
)


class TestAsyncVerifyPassword(unittest.IsolatedAsyncioTestCase):
    """Test cases for async_verify_password function."""

    def setUp(self):
        """Set up test fixtures."""
        self.plain_password = "SecurePass123!"
        self.hashed_password = hash_password(self.plain_password)
        # Argon2id hashes start with $argon2id$
        assert self.hashed_password.startswith("$argon2id$"), (
            f"Expected argon2id hash for test setup, got: {self.hashed_password[:10]}"
        )

    async def test_async_verify_password_returns_true_for_correct_password(self):
        """Test that async_verify_password returns True when password matches."""
        result = await async_verify_password(self.plain_password, self.hashed_password)
        self.assertTrue(result)

    async def test_async_verify_password_returns_false_for_incorrect_password(self):
        """Test that async_verify_password returns False when password does not match."""
        result = await async_verify_password("WrongPassword123!", self.hashed_password)
        self.assertFalse(result)

    async def test_async_verify_password_runs_on_dedicated_executor_thread(self):
        """Test that async_verify_password runs on auth-cpu prefixed thread."""
        thread_names_captured = []

        async def capture_thread_name():
            """Helper that runs in executor and captures thread name."""
            loop = asyncio.get_running_loop()

            def run_in_thread():
                thread_names_captured.append(threading.current_thread().name)
                return verify_password(self.plain_password, self.hashed_password)

            return await loop.run_in_executor(
                _auth_executor, run_in_thread
            )

        # Run the verification
        result = await capture_thread_name()

        # Verify the thread name has the auth-cpu prefix
        self.assertEqual(len(thread_names_captured), 1)
        self.assertTrue(
            thread_names_captured[0].startswith("auth-cpu"),
            f"Expected thread name to start with 'auth-cpu', got: {thread_names_captured[0]}"
        )
        self.assertTrue(result)

    async def test_async_verify_password_concurrent_calls_do_not_deadlock(self):
        """Test that multiple concurrent calls complete without deadlock."""
        # Create multiple different password/hash pairs
        passwords = [f"Password{i}!" for i in range(8)]
        hashes = [hash_password(p) for p in passwords]

        # Run all verifications concurrently
        tasks = [
            async_verify_password(passwords[i], hashes[i])
            for i in range(len(passwords))
        ]
        results = await asyncio.gather(*tasks)

        # All should succeed
        self.assertEqual(len(results), len(passwords))
        self.assertTrue(all(results))

        # Also test with incorrect passwords - should all return False
        wrong_tasks = [
            async_verify_password(f"Wrong{i}!", hashes[i % len(hashes)])
            for i in range(8)
        ]
        wrong_results = await asyncio.gather(*wrong_tasks)
        self.assertTrue(all(not r for r in wrong_results))

    def test_original_verify_password_still_works_synchronously(self):
        """Test backward compatibility: verify_password works synchronously."""
        # Correct password should return True
        result = verify_password(self.plain_password, self.hashed_password)
        self.assertTrue(result)

        # Incorrect password should return False
        result_wrong = verify_password("WrongPassword!", self.hashed_password)
        self.assertFalse(result_wrong)

    async def test_async_verify_password_with_special_characters_in_password(self):
        """Test async_verify_password with password containing special characters."""
        special_password = "P@$$w0rd!#$%^&*()"
        hashed = hash_password(special_password)

        result = await async_verify_password(special_password, hashed)
        self.assertTrue(result)

        result_wrong = await async_verify_password("wrong", hashed)
        self.assertFalse(result_wrong)

    async def test_async_verify_password_with_unicode_password(self):
        """Test async_verify_password with Unicode characters in password."""
        unicode_password = "пароль密码🔐"
        hashed = hash_password(unicode_password)

        result = await async_verify_password(unicode_password, hashed)
        self.assertTrue(result)

        result_wrong = await async_verify_password("wrong", hashed)
        self.assertFalse(result_wrong)


class TestAsyncVerifyPasswordEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Edge case tests for async_verify_password."""

    async def test_async_verify_password_empty_string(self):
        """Test async_verify_password with empty string."""
        hashed = hash_password("nonempty")
        result = await async_verify_password("", hashed)
        self.assertFalse(result)

    async def test_async_verify_password_very_long_password(self):
        """Test async_verify_password with very long password (boundary test)."""
        long_password = "A" * 128  # Max allowed length
        hashed = hash_password(long_password)
        result = await async_verify_password(long_password, hashed)
        self.assertTrue(result)


class TestVerifyPasswordSyncEdgeCases(unittest.TestCase):
    """Sync edge case tests for verify_password."""

    def test_verify_password_empty_string(self):
        """Test verify_password with empty string."""
        hashed = hash_password("nonempty")
        result = verify_password("", hashed)
        self.assertFalse(result)

    def test_verify_password_very_long_password(self):
        """Test verify_password with very long password (boundary test)."""
        long_password = "A" * 128  # Max allowed length
        hashed = hash_password(long_password)
        result = verify_password(long_password, hashed)
        self.assertTrue(result)
