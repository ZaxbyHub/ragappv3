"""Issue #717 acceptance checks — breaker half-open trial + clock source.

RED at base (master), GREEN after the fix.

Two acceptance criteria for ``app/services/circuit_breaker.py``:

- AC7: a HALF_OPEN breaker admits exactly one in-flight trial call; a
  concurrent second caller must be rejected with ``CircuitBreakerError``
  instead of also reaching the provider. At base, ``AsyncCircuitBreaker.
  call`` re-checks only the OPEN state, so every caller that arrives while
  the state is HALF_OPEN is admitted — the "single probe" contract of
  half-open is not enforced.
- AC9: the OPEN -> HALF_OPEN transition must not be pinned shut by a
  backward wall-clock step (e.g. an NTP correction) once the monotonic
  clock has advanced past ``reset_timeout``. At base, ``_check_timeout``
  computes ``time.time() - _last_failure_time``; a backward wall step
  yields a negative elapsed time and the breaker stays OPEN indefinitely.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import circuit_breaker as circuit_breaker_module  # noqa: E402
from app.services.circuit_breaker import (  # noqa: E402
    AsyncCircuitBreaker,
    CircuitBreakerError,
)


class _FakeClock:
    """Double-sided fake of the ``time`` module used by circuit_breaker.

    The wall clock (``time()``) and the monotonic clock (``monotonic()``)
    are independently settable so a test can step the wall clock backward
    while advancing the monotonic clock past a reset timeout — exactly the
    AC9 scenario. Both sides stay controllable so the test remains
    meaningful before and after a fix that switches the breaker onto the
    monotonic clock: the advanced monotonic side must drive the HALF_OPEN
    transition even while the wall side steps backward.
    """

    def __init__(self, wall: float, monotonic: float) -> None:
        self.wall = wall
        self.monotonic_value = monotonic

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value


class TestHalfOpenSingleTrial(unittest.IsolatedAsyncioTestCase):
    """AC7 (issue #717): HALF_OPEN admits exactly one concurrent trial."""

    async def test_half_open_admits_single_trial(self):
        """AC7 (issue #717) — RED at base (master), GREEN after the fix.

        WHEN a breaker is HALF_OPEN and two callers enter concurrently
        while the first trial is still in flight THE SYSTEM SHALL execute
        the wrapped function exactly once and reject the other caller
        with CircuitBreakerError.
        """
        entered = 0
        release = asyncio.Event()

        async def slow_trial():
            nonlocal entered
            entered += 1
            await release.wait()
            return "trial-ok"

        async def provider_down():
            raise RuntimeError("provider down")

        breaker = AsyncCircuitBreaker(fail_max=1, reset_timeout=0.05, name="ac7")

        # One failure is enough to open (fail_max=1).
        with self.assertRaises(RuntimeError):
            await breaker.call(provider_down)
        self.assertEqual(breaker.current_state.value, "open")

        # Let the reset timeout elapse so the next call transitions the
        # breaker to HALF_OPEN (0.15s >> 0.05s).
        await asyncio.sleep(0.15)

        first = asyncio.create_task(breaker.call(slow_trial))
        second = asyncio.create_task(breaker.call(slow_trial))
        try:
            # Give both callers time to pass the gate (or be rejected)
            # while the first trial is parked inside the wrapped function.
            await asyncio.sleep(0.2)
            assert entered == 1
            # The losing caller must have been rejected with
            # CircuitBreakerError, not parked or admitted.
            with self.assertRaises(CircuitBreakerError):
                await second
            release.set()
            self.assertEqual(await asyncio.wait_for(first, timeout=2.0), "trial-ok")
        finally:
            release.set()
            for task in (first, second):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, CircuitBreakerError):
                        pass


class TestBackwardWallClockStep(unittest.IsolatedAsyncioTestCase):
    """AC9 (issue #717): a backward wall step must not pin the breaker OPEN."""

    async def test_backward_wall_clock_step_does_not_pin_open(self):
        """AC9 (issue #717) — RED at base (master), GREEN after the fix.

        WHEN an OPEN breaker's wall clock steps backward (time.time()
        returns 900 after opening at 1000) while monotonic time advances
        past reset_timeout THE SYSTEM SHALL transition the breaker to
        HALF_OPEN on the next timeout check.
        """
        clock = _FakeClock(wall=1000.0, monotonic=5000.0)
        breaker = AsyncCircuitBreaker(fail_max=1, reset_timeout=10.0, name="ac9")

        # circuit_breaker.py does ``import time`` and calls time.time() /
        # time.monotonic() through the module attribute, so replacing the
        # module-level ``time`` binding steers every clock read.
        with mock.patch.object(circuit_breaker_module, "time", clock):
            # Opens the breaker; the open timestamp is taken from the fake
            # clocks (wall 1000 / monotonic 5000).
            breaker.record_failure()
            self.assertEqual(breaker.current_state.value, "open")

            # Wall clock steps 100s backward (NTP correction) while the
            # monotonic clock advances 20s past the open instant;
            # reset_timeout is 10s.
            clock.wall = 900.0
            clock.monotonic_value = 5020.0

            breaker._check_timeout()

        assert breaker.current_state.value == "half_open"


if __name__ == "__main__":
    unittest.main()
