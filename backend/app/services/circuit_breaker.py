"""Circuit breaker utility module for external service resilience.

Provides circuit breaker pattern implementation to prevent
cascading failures when external services become unavailable.
"""

import asyncio
import functools
import logging
import time
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    """Custom exception raised when a circuit breaker is open.

    Raised when a circuit breaker is open and the operation cannot be performed.
    """

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error
        self.message = message

    def __str__(self) -> str:
        if self.original_error:
            return f"{self.message}: {self.original_error}"
        return self.message


class AsyncCircuitBreaker:
    """Async-aware circuit breaker implementation.

    The circuit breaker has three states:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Circuit is tripped, requests fail immediately
    - HALF_OPEN: Testing if service has recovered after timeout
    """

    def __init__(
        self,
        fail_max: int = 5,
        reset_timeout: float = 60,
        success_threshold: int = 1,
        name: Optional[str] = None,
    ):
        """Initialize the circuit breaker.

        Args:
            fail_max: Number of consecutive failures before opening the circuit
            reset_timeout: Seconds to wait before transitioning to half-open
            success_threshold: Number of successes in half-open to close the circuit
            name: Name of the circuit breaker for logging

        Raises:
            ValueError: If fail_max < 1, reset_timeout <= 0, or success_threshold < 1
        """
        if fail_max < 1:
            raise ValueError(f"fail_max must be >= 1, got {fail_max}")
        if reset_timeout <= 0:
            raise ValueError(f"reset_timeout must be > 0, got {reset_timeout}")
        if success_threshold < 1:
            raise ValueError(f"success_threshold must be >= 1, got {success_threshold}")

        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self.success_threshold = success_threshold
        self.name = name or "unnamed"

        self._state = CircuitBreakerState.CLOSED
        self._fail_counter = 0
        self._success_counter = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def current_state(self) -> CircuitBreakerState:
        """Get the current state of the circuit breaker."""
        return self._state

    @property
    def fail_counter(self) -> int:
        """Get the current failure count."""
        return self._fail_counter

    def _transition_to_open(self) -> None:
        """Transition the circuit to OPEN state."""
        self._state = CircuitBreakerState.OPEN
        self._last_failure_time = time.time()
        logger.warning(
            "Circuit breaker '%s' opened after %d consecutive failures",
            self.name,
            self.fail_max,
        )

    def _transition_to_half_open(self) -> None:
        """Transition the circuit to HALF_OPEN state."""
        self._state = CircuitBreakerState.HALF_OPEN
        self._success_counter = 0
        logger.info(
            "Circuit breaker '%s' entering half-open state - testing service",
            self.name,
        )

    def _transition_to_closed(self) -> None:
        """Transition the circuit to CLOSED state."""
        self._state = CircuitBreakerState.CLOSED
        self._fail_counter = 0
        self._success_counter = 0
        self._last_failure_time = None
        logger.info(
            "Circuit breaker '%s' closed - service recovered",
            self.name,
        )

    def reset(self) -> None:
        """Force the circuit breaker back to a clean CLOSED state.

        Intended for use when the underlying endpoint or model is reconfigured
        so a previously opened breaker does not block requests to the new target.
        Caller is responsible for ensuring no concurrent requests are in flight.
        """
        self._state = CircuitBreakerState.CLOSED
        self._fail_counter = 0
        self._success_counter = 0
        self._last_failure_time = None
        logger.info("Circuit breaker '%s' reset to CLOSED", self.name)

    def _check_timeout(self) -> None:
        """Check if the reset timeout has expired and transition to half-open."""
        if self._state == CircuitBreakerState.OPEN:
            if self._last_failure_time is not None:
                elapsed = time.time() - self._last_failure_time
                if elapsed >= self.reset_timeout:
                    self._transition_to_half_open()

    def record_success(self) -> None:
        """Record a successful call."""
        if self._state == CircuitBreakerState.HALF_OPEN:
            self._success_counter += 1
            if self._success_counter >= self.success_threshold:
                self._transition_to_closed()
        elif self._state == CircuitBreakerState.CLOSED:
            # Reset failure counter on success in closed state
            if self._fail_counter > 0:
                self._fail_counter = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        if self._state == CircuitBreakerState.HALF_OPEN:
            # Any failure in half-open goes back to open
            self._transition_to_open()
        elif self._state == CircuitBreakerState.CLOSED:
            self._fail_counter += 1
            if self._fail_counter >= self.fail_max:
                self._transition_to_open()

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a function with circuit breaker protection.

        This allows using the circuit breaker as a decorator or wrapper:
            result = await circuit_breaker(func)(*args, **kwargs)

        Args:
            func: The function to wrap

        Returns:
            A wrapped function that will use circuit breaker protection
        """
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await self.call(func, *args, **kwargs)
        return wrapper

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call a function with circuit breaker protection.

        Args:
            func: The async function to call
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            The result of the function call

        Raises:
            CircuitBreakerError: If the circuit is open
            Exception: Any exception raised by the function
        """
        # Check state under lock
        async with self._lock:
            self._check_timeout()
            if self._state == CircuitBreakerState.OPEN:
                raise CircuitBreakerError(
                    f"Circuit breaker '{self.name}' is open"
                )

        # Execute function without holding lock
        try:
            result = await func(*args, **kwargs)
        except Exception:
            # Record failure under lock
            async with self._lock:
                self.record_failure()
            raise

        # Record success under lock
        async with self._lock:
            self.record_success()
        return result


def circuit_breaker(cb: AsyncCircuitBreaker) -> Callable[[F], F]:
    """Decorator to wrap async functions with circuit breaker protection.

    Args:
        cb: The circuit breaker instance to use for protection.

    Returns:
        A decorator that wraps the function with circuit breaker logic.

    Example:
        @circuit_breaker(embeddings_cb)
        async def get_embeddings(text: str) -> list[float]:
            # Function implementation
            pass
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await cb.call(func, *args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


# Pre-configured circuit breakers for different external services

embeddings_cb = AsyncCircuitBreaker(
    fail_max=5,
    reset_timeout=30,
    name="embeddings",
)

llm_cb = AsyncCircuitBreaker(
    fail_max=5,
    reset_timeout=60,
    name="llm",
)


def create_llm_circuit_breaker(name: str = "llm") -> AsyncCircuitBreaker:
    """Return a fresh per-instance LLM circuit breaker.

    Each LLMClient instance owns its own breaker so failures on one
    backend (e.g. Instant) cannot trip the breaker for another
    (e.g. Thinking).
    """
    return AsyncCircuitBreaker(fail_max=5, reset_timeout=60, name=name)


reranking_cb = AsyncCircuitBreaker(
    fail_max=3,
    reset_timeout=30,
    name="reranking",
)

model_checker_cb = AsyncCircuitBreaker(
    fail_max=3,
    reset_timeout=30,
    name="model_checker",
)
