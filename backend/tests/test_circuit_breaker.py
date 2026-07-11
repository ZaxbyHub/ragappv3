"""Tests for circuit breaker implementation.

Verifies circuit breaker functionality - that it opens after failures
and closes after reset timeout.
"""
import asyncio
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from app.services.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerError,
    CircuitBreakerState,
    circuit_breaker,
    embeddings_cb,
    llm_cb,
    model_checker_cb,
    reranking_cb,
)


class TestCircuitBreakerError:
    """Test suite for CircuitBreakerError exception."""

    def test_error_with_original_error(self):
        """Test CircuitBreakerError with original error."""
        original = ValueError("Original error")
        error = CircuitBreakerError("Circuit is open", original_error=original)

        assert error.message == "Circuit is open"
        assert error.original_error == original
        assert "Circuit is open" in str(error)
        assert "Original error" in str(error)

    def test_error_without_original_error(self):
        """Test CircuitBreakerError without original error."""
        error = CircuitBreakerError("Circuit is open")

        assert error.message == "Circuit is open"
        assert error.original_error is None
        assert str(error) == "Circuit is open"


class TestPreconfiguredCircuitBreakers:
    """Test suite for pre-configured circuit breakers."""

    def test_embeddings_circuit_breaker_config(self):
        """Test embeddings circuit breaker configuration."""
        assert embeddings_cb.name == "embeddings"
        assert embeddings_cb.fail_max == 5
        assert embeddings_cb.reset_timeout == 30

    def test_llm_circuit_breaker_config(self):
        """Test LLM circuit breaker configuration."""
        assert llm_cb.name == "llm"
        assert llm_cb.fail_max == 5
        assert llm_cb.reset_timeout == 60

    def test_reranking_circuit_breaker_config(self):
        """Test reranking circuit breaker configuration."""
        assert reranking_cb.name == "reranking"
        assert reranking_cb.fail_max == 3
        assert reranking_cb.reset_timeout == 30

    def test_model_checker_circuit_breaker_config(self):
        """Test model checker circuit breaker configuration."""
        assert model_checker_cb.name == "model_checker"
        assert model_checker_cb.fail_max == 3
        assert model_checker_cb.reset_timeout == 30


@pytest.mark.asyncio
class TestCircuitBreakerDecorator:
    """Test suite for circuit_breaker decorator."""

    async def test_decorator_passes_through_successful_calls(self):
        """Test that decorator allows successful function calls."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=1,
            name="test_success",
        )

        @circuit_breaker(test_cb)
        async def successful_function():
            return "success"

        result = await successful_function()
        assert result == "success"

    async def test_decorator_counts_failures(self):
        """Test that decorator counts failures and increments failure counter."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=1,
            name="test_failures",
        )

        @circuit_breaker(test_cb)
        async def failing_function():
            raise ValueError("Test error")

        # First two failures should raise ValueError, not CircuitBreakerError
        for _ in range(2):
            with pytest.raises(ValueError):
                await failing_function()

        # Circuit should still be closed
        assert test_cb.current_state == CircuitBreakerState.CLOSED
        assert test_cb.fail_counter == 2

    async def test_circuit_opens_after_max_failures(self):
        """Test that circuit opens after reaching fail_max threshold."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=1,
            name="test_open",
        )

        @circuit_breaker(test_cb)
        async def failing_function():
            raise ValueError("Test error")

        # Trigger 3 failures - first 2 raise ValueError, 3rd opens circuit
        for _ in range(2):
            with pytest.raises(ValueError):
                await failing_function()

        # Circuit should still be closed
        assert test_cb.current_state == CircuitBreakerState.CLOSED

        # 3rd failure opens the circuit
        with pytest.raises(ValueError):
            await failing_function()

        # Circuit should now be open
        assert test_cb.current_state == CircuitBreakerState.OPEN

    async def test_circuit_opens_immediately_on_subsequent_calls(self):
        """Test that circuit opens immediately after threshold is reached."""
        test_cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=60,  # Long timeout to prevent auto-reset
            name="test_immediate_open",
        )

        call_count = 0

        @circuit_breaker(test_cb)
        async def tracked_failing_function():
            nonlocal call_count
            call_count += 1
            raise ValueError(f"Error #{call_count}")

        # First failure raises ValueError
        with pytest.raises(ValueError):
            await tracked_failing_function()

        assert call_count == 1
        assert test_cb.current_state == CircuitBreakerState.CLOSED

        # Second failure opens the circuit
        with pytest.raises(ValueError):
            await tracked_failing_function()

        assert call_count == 2
        assert test_cb.current_state == CircuitBreakerState.OPEN

        # Third call should raise CircuitBreakerError immediately without executing
        with pytest.raises(CircuitBreakerError) as exc_info:
            await tracked_failing_function()

        # Function should not have been called again
        assert call_count == 2
        assert "is open" in str(exc_info.value)

    async def test_circuit_closes_after_reset_timeout(self):
        """Test that circuit closes after reset timeout expires."""
        test_cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=0.1,  # Short timeout for testing
            name="test_reset",
        )

        @circuit_breaker(test_cb)
        async def failing_function():
            raise ValueError("Test error")

        @circuit_breaker(test_cb)
        async def success_function():
            return "recovered"

        # Open the circuit
        for _ in range(2):
            with pytest.raises(ValueError):
                await failing_function()

        assert test_cb.current_state == CircuitBreakerState.OPEN

        # Wait for reset timeout
        await asyncio.sleep(0.15)

        # Circuit should transition to half-open on next call
        # and then close if successful
        result = await success_function()
        assert result == "recovered"
        assert test_cb.current_state == CircuitBreakerState.CLOSED

    async def test_circuit_half_open_state(self):
        """Test that circuit enters half-open state after reset timeout."""
        test_cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=0.1,
            name="test_half_open",
        )

        @circuit_breaker(test_cb)
        async def failing_function():
            raise ValueError("Test error")

        # Open the circuit
        for _ in range(2):
            with pytest.raises(ValueError):
                await failing_function()

        assert test_cb.current_state == CircuitBreakerState.OPEN

        # Wait for reset timeout
        await asyncio.sleep(0.15)

        # The circuit should be in half-open state now
        # The next call will check the timeout and transition
        with pytest.raises(ValueError):
            await failing_function()

        # Circuit should be open again after failure in half-open state
        assert test_cb.current_state == CircuitBreakerState.OPEN

    async def test_decorator_preserves_function_metadata(self):
        """Test that decorator preserves function name and docstring."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=1,
            name="test_metadata",
        )

        @circuit_breaker(test_cb)
        async def my_function():
            """My docstring."""
            return "result"

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."

    async def test_decorator_passes_arguments(self):
        """Test that decorator correctly passes arguments to wrapped function."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=1,
            name="test_args",
        )

        @circuit_breaker(test_cb)
        async def function_with_args(a, b, c=None, **kwargs):
            return {"a": a, "b": b, "c": c, "kwargs": kwargs}

        result = await function_with_args(1, 2, c=3, extra="value")

        assert result["a"] == 1
        assert result["b"] == 2
        assert result["c"] == 3
        assert result["kwargs"] == {"extra": "value"}

    async def test_different_exception_types(self):
        """Test circuit breaker with different exception types."""
        test_cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=1,
            name="test_exceptions",
        )

        @circuit_breaker(test_cb)
        async def value_error_function():
            raise ValueError("Value error")

        @circuit_breaker(test_cb)
        async def type_error_function():
            raise TypeError("Type error")

        # Both exception types should count toward the threshold
        with pytest.raises(ValueError):
            await value_error_function()

        with pytest.raises(TypeError):
            await type_error_function()

        assert test_cb.current_state == CircuitBreakerState.OPEN

    async def test_successful_call_resets_failure_count(self):
        """Test that successful call resets the failure count."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=1,
            name="test_reset_count",
        )

        call_count = 0
        fail_pattern = [True, True, False]  # 2 fails then 1 success

        @circuit_breaker(test_cb)
        async def conditional_function():
            nonlocal call_count
            should_fail = fail_pattern[call_count % len(fail_pattern)]
            call_count += 1
            if should_fail:
                raise ValueError("Error")
            return "success"

        # Two failures
        for _ in range(2):
            with pytest.raises(ValueError):
                await conditional_function()

        # Circuit should still be closed
        assert test_cb.current_state == CircuitBreakerState.CLOSED
        assert test_cb.fail_counter == 2

        # Make it succeed
        result = await conditional_function()

        assert result == "success"
        assert test_cb.current_state == CircuitBreakerState.CLOSED
        assert test_cb.fail_counter == 0  # Reset after success

        # Make it fail again - should need 3 more failures to open
        fail_pattern = [True, True]  # 2 more fails
        for _ in range(2):
            with pytest.raises(ValueError):
                await conditional_function()

        # Circuit should still be closed after 2 more failures
        # (because the success reset the count)
        assert test_cb.current_state == CircuitBreakerState.CLOSED


@pytest.mark.asyncio
class TestCircuitBreakerIntegration:
    """Integration tests for circuit breaker with realistic scenarios."""

    async def test_multiple_successful_calls_keep_circuit_closed(self):
        """Test that multiple successful calls keep circuit closed."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=1,
            name="test_multiple_success",
        )

        @circuit_breaker(test_cb)
        async def reliable_function():
            return "always works"

        # Many successful calls
        for _ in range(10):
            result = await reliable_function()
            assert result == "always works"

        assert test_cb.current_state == CircuitBreakerState.CLOSED
        assert test_cb.fail_counter == 0

    async def test_mixed_success_and_failures(self):
        """Test circuit behavior with mixed success and failure pattern."""
        test_cb = AsyncCircuitBreaker(
            fail_max=5,
            reset_timeout=1,
            name="test_mixed",
        )

        call_count = 0
        fail_pattern = [False, True, False, True, False]  # Mixed pattern

        @circuit_breaker(test_cb)
        async def mixed_function():
            nonlocal call_count
            should_fail = fail_pattern[call_count % len(fail_pattern)]
            call_count += 1
            if should_fail:
                raise ValueError("Error")
            return "success"

        # Run multiple times with mixed pattern
        results = []
        errors = []
        for _ in range(10):
            try:
                result = await mixed_function()
                results.append(result)
            except ValueError:
                errors.append("error")

        # Should have some successes and some failures
        # But circuit should never open because we never get 5 consecutive failures
        assert len(results) > 0
        assert test_cb.current_state == CircuitBreakerState.CLOSED

    async def test_circuit_breaker_isolation(self):
        """Test that different circuit breakers are isolated from each other."""
        cb1 = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=1,
            name="cb1",
        )
        cb2 = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=1,
            name="cb2",
        )

        @circuit_breaker(cb1)
        async def function1():
            raise ValueError("Error 1")

        @circuit_breaker(cb2)
        async def function2():
            raise ValueError("Error 2")

        # Open cb1
        for _ in range(2):
            with pytest.raises(ValueError):
                await function1()

        assert cb1.current_state == CircuitBreakerState.OPEN
        assert cb2.current_state == CircuitBreakerState.CLOSED

        # cb2 should still work (or fail normally, not with circuit open)
        with pytest.raises(ValueError):
            await function2()

        # cb2 should still be closed after one failure
        assert cb2.current_state == CircuitBreakerState.CLOSED


@pytest.mark.asyncio
class TestCircuitBreakerStateTransitions:
    """Test suite for circuit breaker state transitions."""

    async def test_state_closed_to_open_transition(self):
        """Test transition from CLOSED to OPEN state."""
        test_cb = AsyncCircuitBreaker(
            fail_max=3,
            reset_timeout=60,
            name="test_closed_to_open",
        )

        # Initial state should be CLOSED
        assert test_cb.current_state == CircuitBreakerState.CLOSED

        @circuit_breaker(test_cb)
        async def failing_function():
            raise ValueError("Error")

        # Trigger failures to open circuit
        for _ in range(3):
            with pytest.raises(ValueError):
                await failing_function()

        # State should now be OPEN
        assert test_cb.current_state == CircuitBreakerState.OPEN

    async def test_state_open_to_half_open_transition(self):
        """Test transition from OPEN to HALF_OPEN after timeout."""
        test_cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=0.1,  # Short timeout for testing
            name="test_open_to_half_open",
        )

        @circuit_breaker(test_cb)
        async def failing_function():
            raise ValueError("Error")

        # Open the circuit
        for _ in range(2):
            with pytest.raises(ValueError):
                await failing_function()

        assert test_cb.current_state == CircuitBreakerState.OPEN

        # Wait for reset timeout
        await asyncio.sleep(0.15)

        # The next call will trigger transition to HALF_OPEN
        # Since the function fails, it goes back to OPEN
        with pytest.raises(ValueError):
            await failing_function()

    async def test_state_half_open_to_closed_on_success(self):
        """Test transition from HALF_OPEN to CLOSED on successful call."""
        test_cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=0.1,
            success_threshold=1,
            name="test_half_open_to_closed",
        )

        call_count = 0

        @circuit_breaker(test_cb)
        async def recovery_function():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ValueError("Error")
            return "recovered"

        # Open the circuit
        for _ in range(2):
            with pytest.raises(ValueError):
                await recovery_function()

        assert test_cb.current_state == CircuitBreakerState.OPEN

        # Wait for reset timeout
        await asyncio.sleep(0.15)

        # Success in half-open should close the circuit
        result = await recovery_function()
        assert result == "recovered"
        assert test_cb.current_state == CircuitBreakerState.CLOSED


@pytest.mark.asyncio
class TestAsyncCircuitBreakerDirect:
    """Test suite for AsyncCircuitBreaker class directly."""

    async def test_direct_call_success(self):
        """Test direct call method with success."""
        cb = AsyncCircuitBreaker(fail_max=3, reset_timeout=1, name="direct_test")

        async def success_func():
            return "success"

        result = await cb.call(success_func)
        assert result == "success"
        assert cb.fail_counter == 0

    async def test_direct_call_failure(self):
        """Test direct call method with failure."""
        cb = AsyncCircuitBreaker(fail_max=3, reset_timeout=1, name="direct_test")

        async def fail_func():
            raise ValueError("error")

        with pytest.raises(ValueError):
            await cb.call(fail_func)

        assert cb.fail_counter == 1

    async def test_direct_call_opens_circuit(self):
        """Test that direct call opens circuit after max failures."""
        cb = AsyncCircuitBreaker(fail_max=2, reset_timeout=60, name="direct_test")

        async def fail_func():
            raise ValueError("error")

        # First failure
        with pytest.raises(ValueError):
            await cb.call(fail_func)
        assert cb.current_state == CircuitBreakerState.CLOSED

        # Second failure opens circuit
        with pytest.raises(ValueError):
            await cb.call(fail_func)
        assert cb.current_state == CircuitBreakerState.OPEN

        # Third call raises CircuitBreakerError
        with pytest.raises(CircuitBreakerError):
            await cb.call(fail_func)

    async def test_record_success_in_half_open(self):
        """Test record_success in half-open state."""
        cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=0.1,
            success_threshold=1,
            name="half_open_test"
        )

        # Open the circuit
        cb._transition_to_open()
        assert cb.current_state == CircuitBreakerState.OPEN

        # Wait for timeout
        await asyncio.sleep(0.15)

        # Check timeout transitions to half-open
        cb._check_timeout()
        assert cb.current_state == CircuitBreakerState.HALF_OPEN

        # Record success should close circuit
        cb.record_success()
        assert cb.current_state == CircuitBreakerState.CLOSED

    async def test_record_failure_in_half_open(self):
        """Test record_failure in half-open state."""
        cb = AsyncCircuitBreaker(
            fail_max=2,
            reset_timeout=0.1,
            name="half_open_test"
        )

        # Open the circuit
        cb._transition_to_open()
        assert cb.current_state == CircuitBreakerState.OPEN

        # Wait for timeout and transition to half-open
        await asyncio.sleep(0.15)
        cb._check_timeout()
        assert cb.current_state == CircuitBreakerState.HALF_OPEN

        # Record failure should open circuit again
        cb.record_failure()
        assert cb.current_state == CircuitBreakerState.OPEN

    async def test_callable_wrapper_pattern(self):
        """Test using circuit breaker as a callable wrapper (e.g., cb(func)(args))."""
        cb = AsyncCircuitBreaker(fail_max=2, reset_timeout=60, name="callable_test")

        async def test_func(arg1, arg2):
            return f"result: {arg1}, {arg2}"

        # Use the circuit breaker as a callable wrapper
        wrapped = cb(test_func)
        result = await wrapped("a", "b")
        assert result == "result: a, b"
        assert cb.fail_counter == 0

    async def test_callable_wrapper_with_failure(self):
        """Test callable wrapper pattern with failures."""
        cb = AsyncCircuitBreaker(fail_max=2, reset_timeout=60, name="callable_fail_test")

        async def failing_func():
            raise ValueError("error")

        # Use the circuit breaker as a callable wrapper
        wrapped = cb(failing_func)

        # First failure
        with pytest.raises(ValueError):
            await wrapped()
        assert cb.fail_counter == 1

        # Second failure opens circuit
        with pytest.raises(ValueError):
            await wrapped()
        assert cb.current_state == CircuitBreakerState.OPEN

        # Third call raises CircuitBreakerError
        with pytest.raises(CircuitBreakerError):
            await wrapped()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
