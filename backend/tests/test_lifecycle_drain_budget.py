"""Issue #854 acceptance checks: BackgroundProcessor.stop() shutdown budget.

stop() must enforce ONE aggregate deadline across its composed drain
sequence, not the full ``timeout`` per phase. RED at base, GREEN after the
fix. Line numbers from base, may drift.

Stalled phases and how (see each test for the exact combination):

- Phase "ingestion queue join" (``await asyncio.wait_for(self.queue.join(),
  timeout=timeout)``, background_tasks.py ~:3444): in the composed/drains
  scenarios slowed to INGESTION_DRAIN_SECONDS (0.9 s) but still COMPLETING.
  Deliberate: a fully-stalled first queue times out, sets
  ``queue_drained = False``, and stop() then SKIPS the enrichment/atom joins
  entirely — only a slow-but-successful drain walks every bounded arm.
- Phase "enrichment queue join" (~:3453) and "atom enrichment queue join"
  (~:3461): joinables whose ``join()`` sleeps STALL_SECONDS (10 s) with no
  consumer, so each wait_for burns the full timeout before cancelling.
- Phase "ingest-lease settle" (``_wait_ingest_jobs_settled()``, ~:3472):
  overridden to sleep STALL_SECONDS, burning the full timeout.
- Trailing ``vector_store.flush_optimize()`` (~:3596): awaited with NO
  timeout at base (the unbounded arm); the slow store sleeps
  FLUSH_STALL_SECONDS (5 s).
- Maintenance ``asyncio.to_thread`` calls (lease release ~:3535): awaited
  with NO timeout at base; C12-MAINT patches the lease-release callable to
  sleep STALL_SECONDS (10 s) with everything else fast.

Discrimination (bound = BUDGET_SECONDS + SLACK_SECONDS = 2.5 s):

- C12-COMPOSED (all slow): base elapsed ~= 0.9 + 3 x 1.0 + 5.0 = 8.9 s.
- C12-DRAINS (three joins stall, flush fast): base ~= 0.9 + 3 x 1.0 = 3.9 s.
- C12-OPTIMIZE (joins fast/empty, flush stalls 5 s): base ~= 5.0 s.
- C12-FAST (nothing slow) must pass before and after the fix.

The three RED tests separate the PARTIAL fixes: aggregating only the drain
phases still leaves C12-COMPOSED and C12-OPTIMIZE red (the unbounded
optimize dominates); bounding only the optimize still leaves C12-COMPOSED
and C12-DRAINS red (the per-phase drains dominate). Only a fix that bounds
the whole composed sequence inside one deadline greens all three.
"""

import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BUDGET_SECONDS = 1.0
SLACK_SECONDS = 1.5
BOUND_SECONDS = BUDGET_SECONDS + SLACK_SECONDS  # 2.5
INGESTION_DRAIN_SECONDS = 0.9
STALL_SECONDS = 10.0
FLUSH_STALL_SECONDS = 5.0


class _SlowJoinable:
    """Queue stand-in whose ``join()`` blocks for ``drain_seconds``."""

    def __init__(self, drain_seconds: float):
        self._drain_seconds = drain_seconds

    async def join(self):
        await asyncio.sleep(self._drain_seconds)


class _SlowVectorStore:
    """Vector store double whose optimize stalls ``stall_seconds``.

    ``entered`` records that flush_optimize() was actually CALLED: the
    bounded-optimize assertions are only meaningful when the optimize
    branch ran (a stop implementation that skipped it would otherwise
    satisfy the timing assertion).
    """

    def __init__(self, stall_seconds: float):
        self._stall_seconds = stall_seconds
        self.entered = False

    async def flush_optimize(self):
        self.entered = True
        await asyncio.sleep(self._stall_seconds)


def _build_minimal_processor(
    background_processor_cls,
    *,
    queue_drain_seconds: float,
    join_stall_seconds: float,
    flush_stall_seconds: float,
):
    """Lightest faithful instance that still runs the REAL stop().

    The subclass overrides ONLY ``__init__`` (no ``super().__init__`` — the
    production comment at stop()'s tail explicitly supports "minimal
    processors without running ``__init__``") plus the one lease method the
    stalled phase uses; ``stop()`` itself is inherited and unmodified. No
    real workers are started. The four awaited drain joinables
    (queue/enrichment/atom/lease-settle) take ``queue_drain_seconds`` /
    ``join_stall_seconds`` and the trailing flush_optimize takes
    ``flush_stall_seconds``.
    """

    class _MinimalProcessor(background_processor_cls):
        def __init__(self):
            # Slow-but-completing first queue so the drain walks EVERY
            # bounded arm (see module docstring).
            self.queue = _SlowJoinable(drain_seconds=queue_drain_seconds)
            self.enrichment_queue = _SlowJoinable(drain_seconds=join_stall_seconds)
            self.atom_enrichment_queue = _SlowJoinable(
                drain_seconds=join_stall_seconds
            )
            self.shutdown_event = asyncio.Event()
            self.vector_store = _SlowVectorStore(
                stall_seconds=flush_stall_seconds
            )
            self.processor = SimpleNamespace(
                vector_store=self.vector_store,
                pool=None,
            )
            self._worker_tasks = ()
            self._enrichment_worker_task = None
            self._vector_delete_sweep_task = None
            self._reindex_worker_task = None
            self._ingest_lease_enabled = True
            self._running = True

        async def _wait_ingest_jobs_settled(self):
            # Phase 4 stall (lease settle): burn the full wait_for timeout.
            await asyncio.sleep(join_stall_seconds)

        def _release_all_ingest_leases(self):
            # Touched (via to_thread) after the lease settle when
            # _ingest_lease_enabled is True; nothing to release on a stub.
            return 0

    return _MinimalProcessor()


def _make_processor():
    from app.services.background_tasks import BackgroundProcessor

    return BackgroundProcessor


async def _run_stop_and_measure(processor, timeout: float) -> float:
    from app.config import settings

    started = time.monotonic()
    with patch.object(settings, "optimize_on_shutdown", True):
        await processor.stop(timeout=timeout)
    return time.monotonic() - started


class TestStopHonorsOneAggregateDeadline(unittest.IsolatedAsyncioTestCase):
    """#854: the composed drain sequence must fit one budget."""

    async def test_composed_worst_case_exceeds_single_budget(self):
        """All four drain phases slow AND the trailing optimize slow."""
        processor = _build_minimal_processor(
            _make_processor(),
            queue_drain_seconds=INGESTION_DRAIN_SECONDS,
            join_stall_seconds=STALL_SECONDS,
            flush_stall_seconds=FLUSH_STALL_SECONDS,
        )
        elapsed = await _run_stop_and_measure(processor, BUDGET_SECONDS)
        # NOTE: the FROZEN ORACLE assertion (the elapsed bound) comes FIRST:
        # it is what makes this check discriminating at the base, so nothing
        # may shadow it.
        self.assertLessEqual(
            elapsed,
            BOUND_SECONDS,
            f"C12-COMPOSED elapsed={elapsed:.2f}s bound={BOUND_SECONDS:.1f}s "
            f"— stop(timeout={BUDGET_SECONDS}s) with every drain phase slow "
            "and the optimize slow took far longer than one budget: the "
            "phases are granted the timeout EACH plus an unbounded "
            "flush_optimize wait, so the composed worst case exceeds any "
            "single grace period (docker-compose stop_grace_period 90s < "
            "4x60s) and a busy shutdown is SIGKILLed mid-drain. stop() must "
            "enforce one aggregate deadline across the whole drain sequence "
            "(or the Compose grace must be sized for the measured composed "
            "worst case).",
        )
        # The composed case reaches the optimize at all: the aggregate
        # deadline is CONSUMED by the slow drain phases, so the trailing
        # optimize is never started. That is the property the composed
        # case proves, so it is asserted directly (the isolated optimize
        # case is the one that must reach the branch).
        self.assertFalse(
            processor.vector_store.entered,
            "the aggregate deadline must be consumed by the drain phases, so "
            "the trailing optimize is never started (the composed worst case "
            "is one budget, not a budget per phase)",
        )

    async def test_drain_phases_share_one_budget(self):
        """Only the three stalled joins (enrichment, atom, lease settle);
        the trailing optimize is fast — isolating the drain-phase budget."""
        processor = _build_minimal_processor(
            _make_processor(),
            queue_drain_seconds=INGESTION_DRAIN_SECONDS,
            join_stall_seconds=STALL_SECONDS,
            flush_stall_seconds=0.0,
        )
        elapsed = await _run_stop_and_measure(processor, BUDGET_SECONDS)
        self.assertLessEqual(
            elapsed,
            BOUND_SECONDS,
            f"C12-DRAINS elapsed={elapsed:.2f}s bound={BOUND_SECONDS:.1f}s "
            f"— with the three drain joins stalled (enrichment, atom, lease "
            f"settle) and a fast optimize, stop(timeout={BUDGET_SECONDS}s) "
            "still consumed the timeout PER PHASE (~4x the budget). The "
            "drain phases must share ONE aggregate deadline.",
        )

    async def test_flush_optimize_is_bounded(self):
        """Joins fast/empty; only the trailing flush_optimize stalls —
        isolating the unbounded optimize arm."""
        processor = _build_minimal_processor(
            _make_processor(),
            queue_drain_seconds=0.0,
            join_stall_seconds=0.0,
            flush_stall_seconds=FLUSH_STALL_SECONDS,
        )
        elapsed = await _run_stop_and_measure(processor, BUDGET_SECONDS)
        self.assertTrue(
            processor.vector_store.entered,
            "flush_optimize() must actually have been called — otherwise "
            "this test proves nothing about the bounded optimize",
        )
        self.assertLessEqual(
            elapsed,
            BOUND_SECONDS,
            f"C12-OPTIMIZE elapsed={elapsed:.2f}s bound={BOUND_SECONDS:.1f}s "
            f"— with all joins fast and only flush_optimize slow, "
            f"stop(timeout={BUDGET_SECONDS}s) waited the optimize out in "
            "full: flush_optimize() is awaited with NO timeout, so a slow "
            "optimize alone can blow the shutdown budget. It must sit "
            "inside the same aggregate deadline as the drains.",
        )

    async def test_maintenance_to_thread_is_bounded(self):
        """Joins fast/empty and a fast optimize, but the maintenance lease
        release (awaited via asyncio.to_thread after worker cancellation)
        sleeps 10 s; at base that await is unbounded, so stop() waits it
        out in full."""
        processor = _build_minimal_processor(
            _make_processor(),
            queue_drain_seconds=0.0,
            join_stall_seconds=0.0,
            flush_stall_seconds=0.0,
        )

        def _sleepy_release() -> int:
            # Sync maintenance work far beyond the budget, executed by
            # stop() via asyncio.to_thread (thread sleep, not loop sleep).
            time.sleep(STALL_SECONDS)
            return 0

        # stop() reads the INSTANCE attribute at call time
        # (``await asyncio.to_thread(self._release_all_ingest_leases)``),
        # so this override is exactly what runs; the patched function never
        # touches processor.pool, so the SimpleNamespace pool=None rig is
        # fine here.
        processor._release_all_ingest_leases = _sleepy_release  # noqa: SLF001
        elapsed = await _run_stop_and_measure(processor, BUDGET_SECONDS)
        self.assertLessEqual(
            elapsed,
            BOUND_SECONDS,
            f"C12-MAINT elapsed={elapsed:.2f}s bound={BOUND_SECONDS:.1f}s "
            f"— with all joins fast and a fast optimize, the maintenance "
            f"to_thread lease-release patched to sleep {STALL_SECONDS:.0f}s "
            f"made stop(timeout={BUDGET_SECONDS}s) wait it out in full: "
            "the maintenance asyncio.to_thread awaits are unbounded at "
            "base and must sit inside the same aggregate deadline "
            "(remaining-budget bound)",
        )

    async def test_fast_drain_returns_promptly(self):
        """Nothing slow: empty/fast queues and instant optimize — stop()
        must complete well inside the budget, before and after the fix."""
        processor = _build_minimal_processor(
            _make_processor(),
            queue_drain_seconds=0.0,
            join_stall_seconds=0.0,
            flush_stall_seconds=0.0,
        )
        elapsed = await _run_stop_and_measure(processor, BUDGET_SECONDS)
        self.assertLess(
            elapsed,
            BOUND_SECONDS,
            f"C12-FAST elapsed={elapsed:.2f}s bound={BOUND_SECONDS:.1f}s "
            "— with nothing stalled, stop() must return promptly; a slow "
            "return here means the fix added unconditional waiting.",
        )
        self.assertFalse(
            processor._running,  # noqa: SLF001 - contract of a completed stop
            "C12-FAST running-flag=True — stop() must mark the processor "
            "not-running on the fast path",
        )


if __name__ == "__main__":
    unittest.main()
