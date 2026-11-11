"""Arm's-length acceptance checks for the draft-capability-liveness trace
(issues #902/#905/#911/#913), capability side.

Frozen specification for honest capability reporting. Every DISCRIMINATING
check below is RED at base, GREEN after the fix; the explicitly marked
guardrail legs stay GREEN once the field/flag exists.

Coverage map:

* C1 / ``TestN05CPromoteAvailability`` -- ``promote_available`` must reflect
  whether ``app.state.background_processor`` holds a LIVE processor: None
  -> false; a REAL ``BackgroundProcessor`` that is not running (constructed
  and never started, or left mounted after its ``start()`` raised — the
  production shape lifespan's ``_safe_await`` swallows at
  ``backend/app/lifespan.py``) -> false; a bare stub object -> true.
* C6 / ``TestN05CUploadAvailability`` -- capabilities must expose a NEW
  boolean ``upload_available`` derived from
  ``app.state.draft_job_processor`` liveness: None -> false; a REAL
  ``DraftJobProcessor`` mounted after a FAILED ``start()`` -> false; a
  bare stub object -> true.
* C7 / ``TestN05CProcessorLiveness`` -- with a REAL ``DraftJobProcessor``
  mounted on ``app.state.draft_job_processor`` (mounted BEFORE the loop is
  killed, so a done-callback design and a task-probe design both satisfy
  the contract) whose poll-loop task has terminated after a successful
  start, capabilities must report BOTH ``compile_available`` and
  ``upload_available`` false; a REAL processor whose ``start()`` FAILED
  reports both false; a live poll loop reports both true. The poll loop is
  killed by raising ``PollLoopDeath`` (a custom ``BaseException``
  subclass, deliberately NOT SystemExit/KeyboardInterrupt and not an
  ``Exception``, so it escapes the loop's own ``except Exception``
  handler) from the job-claim step.

Harness: subclasses ``CompileRouteTestBase`` (settings isolation, dependency
overrides, pooled SQLite fixture, auth headers). Real-processor legs mount
the processor directly on ``app.state`` — never through lifespan wiring —
and drive its poll loop on a private background event loop so the loop (and
its task) stays alive across the capabilities request made through
``TestClient``.
"""

import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.main import app
from app.services.background_tasks import BackgroundProcessor
from app.services.draft_job_processor import DraftJobProcessor
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase


class PollLoopDeath(BaseException):
    """Simulated poll-loop crash.

    ``BaseException`` (and not ``Exception``) so the poll loop's own broad
    ``except Exception`` handler cannot absorb it, and not
    SystemExit/KeyboardInterrupt/CancelledError so nothing else in the
    machinery treats it as a normal shutdown.
    """


def set_app_state(name, value):
    """Install ``app.state.<name> = value`` and return a restore callable.

    The attribute may legitimately be absent (lifespan never ran), so the
    restore deletes it again in that case.
    """
    had = hasattr(app.state, name)
    previous = getattr(app.state, name, None)
    setattr(app.state, name, value)

    def _restore():
        if had:
            setattr(app.state, name, previous)
        else:
            delattr(app.state, name)

    return _restore


def build_real_processor(test):
    """A REAL ``DraftJobProcessor`` bound to the route base's pooled SQLite
    fixture (the pool exposes the ``with pool.connection()`` idiom the
    processor requires) and the test's temp data dir."""
    from app.services.draft_input_storage import DraftInputStorage
    from app.services.draft_job_processor import DraftJobProcessor
    from tests.draft_room.test_draft_job_processor import FakeExtractionService

    return DraftJobProcessor(
        pool=test._connection_pool,
        storage=DraftInputStorage(Path(settings.data_dir) / "draft-room"),
        extraction=FakeExtractionService(),
        poll_interval=0.05,
    )


def start_processor_on_worker_loop(processor):
    """Start ``processor`` on a private background event loop.

    The poll-loop task must outlive ``start()``'s coroutine; a plain
    ``asyncio.run`` would tear the loop (and the task) down on return, so
    the loop runs in a daemon thread until ``stop_worker_loop``.
    Returns ``(loop, thread)``.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def _start():
        await processor.start()

    asyncio.run_coroutine_threadsafe(_start(), loop).result(timeout=30)
    return loop, thread


def stop_worker_loop(processor, loop, thread):
    """Stop the processor, then the private loop and its thread."""
    if processor is not None:

        async def _stop():
            await processor.stop()

        asyncio.run_coroutine_threadsafe(_stop(), loop).result(timeout=30)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=30)


async def wait_until_task_done(task, timeout=10.0):
    """Event-driven wait for ``task`` to terminate (no polling sleeps)."""
    if task.done():
        return
    done = asyncio.Event()
    task.add_done_callback(lambda _task: done.set())
    await asyncio.wait_for(done.wait(), timeout=timeout)


async def wait_for_first_claim(claim_event, loop, timeout=15.0):
    """Event-driven wait until the patched claim step has run once (proof the
    poll loop is alive and iterating)."""
    await asyncio.wait_for(
        loop.run_in_executor(None, claim_event.wait, timeout), timeout=timeout
    )


def start_and_expect_failure(processor):
    """Drive ``processor.start()`` on a throwaway loop and REQUIRE it to
    raise — the production shape lifespan's ``_safe_await`` swallows
    (backend/app/lifespan.py:432-439), leaving the not-running object
    mounted on app.state with no worker tasks behind it."""

    async def _try_start():
        await processor.start()

    try:
        asyncio.run(_try_start())
    except BaseException:
        return
    raise AssertionError("the poisoned start() unexpectedly succeeded")


class _CapabilityLivenessBase(CompileRouteTestBase):
    def _capabilities(self):
        resp = self.client.get(
            "/api/draft-room/capabilities", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()


class TestN05CPromoteAvailability(_CapabilityLivenessBase):
    """C1 (DISCRIMINATING)."""

    def test_promote_unavailable_without_a_background_processor(self):
        """RED at base (the literal True), GREEN after the fix: with
        ``app.state.background_processor`` None — exactly what lifespan
        leaves behind when the background processor fails to start —
        capabilities must stop advertising promotion as available, because
        a promote would enqueue ingestion into nothing."""
        restore = set_app_state("background_processor", None)
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIs(
            body["promote_available"],
            False,
            "promote_available must be false when app.state.background_processor "
            "is None (no worker can ingest a promoted document)",
        )

    def test_promote_available_with_a_processor_object_present(self):
        """Guardrail (GREEN once the fix lands): with a stub processor object
        on app state, promotion is available."""
        restore = set_app_state("background_processor", object())
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIs(
            body["promote_available"],
            True,
            "promote_available must be true when app.state.background_processor "
            "holds a processor object",
        )

    def test_promote_unavailable_with_a_real_processor_that_never_started(self):
        """RED at base (the literal True), GREEN after the fix: a REAL
        ``BackgroundProcessor`` constructed but never started has no worker
        loops behind it — a promote would enqueue ingestion into nothing, so
        capabilities must not advertise promotion as available."""
        restore = set_app_state("background_processor", BackgroundProcessor())
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIs(
            body["promote_available"],
            False,
            "promote_available must be false when a REAL BackgroundProcessor is "
            "mounted but was never started (no worker loops are running)",
        )

    def test_promote_unavailable_with_a_real_processor_whose_start_failed(self):
        """RED at base (the literal True), GREEN after the fix: production's
        lifespan swallows a ``BackgroundProcessor.start()`` failure and
        leaves the not-running object mounted; capabilities must treat that
        object as no worker, not as a live one."""
        processor = BackgroundProcessor()
        with patch.object(
            BackgroundProcessor, "_run_startup_recovery", lambda self: "not-awaitable"
        ):
            # start() must raise while spawning the recovery task, reset
            # _running, and leave the (not-running) object intact.
            start_and_expect_failure(processor)
        restore = set_app_state("background_processor", processor)
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIs(
            body["promote_available"],
            False,
            "promote_available must be false when a REAL BackgroundProcessor is "
            "mounted after its start() raised (lifespan swallows the failure and "
            "leaves the not-running object on app.state)",
        )


class TestN05CUploadAvailability(_CapabilityLivenessBase):
    """C6 (DISCRIMINATING)."""

    def test_upload_unavailable_without_a_job_processor(self):
        """RED at base (the field does not exist yet), GREEN after the fix:
        upload is only end-to-end usable when a worker exists to parse the
        uploaded input, so capabilities must expose ``upload_available``
        false when ``app.state.draft_job_processor`` is None."""
        restore = set_app_state("draft_job_processor", None)
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIn(
            "upload_available",
            body,
            "capabilities must expose an upload_available field derived from "
            "app.state.draft_job_processor liveness",
        )
        self.assertIs(
            body["upload_available"],
            False,
            "upload_available must be false when app.state.draft_job_processor "
            "is None (no worker will ever parse the uploaded input)",
        )

    def test_upload_available_with_a_processor_stub(self):
        """Guardrail (GREEN once the fix lands): with a stub processor object
        on app state, upload is available."""
        restore = set_app_state("draft_job_processor", object())
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIn("upload_available", body)
        self.assertIs(
            body["upload_available"],
            True,
            "upload_available must be true when app.state.draft_job_processor "
            "holds a processor object",
        )

    def test_upload_unavailable_with_a_real_processor_whose_start_failed(self):
        """RED at base (the field does not exist yet), GREEN after the fix:
        production's lifespan leaves ``app.state.draft_job_processor``
        mounted even when ``DraftJobProcessor.start()`` raised (the
        ``_safe_await`` swallow) — that object has no poll loop, so nothing
        would ever parse an uploaded input."""
        processor = build_real_processor(self)
        with patch.object(
            DraftJobProcessor,
            "_recover_on_startup",
            side_effect=RuntimeError("simulated startup recovery failure"),
        ):
            start_and_expect_failure(processor)
        self.assertFalse(processor._running)
        restore = set_app_state("draft_job_processor", processor)
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIn(
            "upload_available",
            body,
            "capabilities must expose an upload_available field derived from "
            "app.state.draft_job_processor liveness",
        )
        self.assertIs(
            body["upload_available"],
            False,
            "upload_available must be false when a REAL DraftJobProcessor is "
            "mounted after its start() failed (no poll loop will ever claim the "
            "parse job an upload enqueues)",
        )


class TestN05CProcessorLiveness(_CapabilityLivenessBase):
    """C7 (DISCRIMINATING): capability flags must track the REAL worker's
    poll-loop liveness, not merely the presence of the object."""

    def setUp(self):
        super().setUp()
        self._loop = None
        self._thread = None
        self._processor = None
        self._restore_state = None

    def tearDown(self):
        if self._loop is not None:
            stop_worker_loop(self._processor, self._loop, self._thread)
        if self._restore_state is not None:
            self._restore_state()
        super().tearDown()

    def _mount(self, processor):
        self._processor = processor
        self._restore_state = set_app_state("draft_job_processor", processor)

    def test_dead_poll_loop_reports_compile_and_upload_unavailable(self):
        """RED at base (both flags are presence-derived or absent, so a
        crashed worker still advertises compile/upload), GREEN after the
        fix: a REAL processor whose poll-loop task terminated after a
        successful start must not advertise compile or upload. The processor
        is MOUNTED on app.state BEFORE the loop is killed, so both a design
        that clears app state from a done-callback and a design that probes
        the poll-loop task satisfy the contract."""
        processor = build_real_processor(self)
        # Mount FIRST: app.state points at the real processor throughout the
        # crash, exactly as it does in production when a loop dies mid-run.
        self._mount(processor)
        self._loop, self._thread = start_processor_on_worker_loop(processor)

        def _claim_then_die(inner_self):
            raise PollLoopDeath("simulated poll-loop crash inside the claim step")

        with patch.object(DraftJobProcessor, "_claim_next_job", _claim_then_die):

            async def _drive():
                await wait_until_task_done(processor._task)

            asyncio.run_coroutine_threadsafe(_drive(), self._loop).result(timeout=30)

        task = processor._task
        self.assertIsNotNone(task, "a successful start must publish a poll-loop task")
        self.assertTrue(
            task.done(),
            "the poll-loop task must have terminated after the injected crash",
        )
        self.assertFalse(
            task.cancelled(),
            "the poll loop must have died from the injected BaseException, not "
            "from a cancellation",
        )
        self.assertIsInstance(
            task.exception(),
            PollLoopDeath,
            "the poll loop must have died from the injected BaseException",
        )

        body = self._capabilities()
        self.assertIs(
            body["compile_available"],
            False,
            "compile_available must be false when the real draft job "
            "processor's poll loop has terminated",
        )
        self.assertIn(
            "upload_available",
            body,
            "capabilities must expose an upload_available field derived from "
            "app.state.draft_job_processor liveness",
        )
        self.assertIs(
            body["upload_available"],
            False,
            "upload_available must be false when the real draft job "
            "processor's poll loop has terminated",
        )

    def test_failed_start_reports_compile_and_upload_unavailable(self):
        """RED at base (compile_available is presence-derived True, upload
        field absent), GREEN after the fix: a REAL processor mounted after
        its ``start()`` RAISED (the lifespan ``_safe_await`` swallow) has no
        poll loop at all and must advertise neither compile nor upload."""
        processor = build_real_processor(self)
        with patch.object(
            DraftJobProcessor,
            "_recover_on_startup",
            side_effect=RuntimeError("simulated startup recovery failure"),
        ):
            start_and_expect_failure(processor)
        self.assertFalse(processor._running)
        self.assertIsNone(processor._task)

        self._mount(processor)
        body = self._capabilities()
        self.assertIs(
            body["compile_available"],
            False,
            "compile_available must be false when the real draft job "
            "processor's start() failed (no poll loop exists)",
        )
        self.assertIn(
            "upload_available",
            body,
            "capabilities must expose an upload_available field derived from "
            "app.state.draft_job_processor liveness",
        )
        self.assertIs(
            body["upload_available"],
            False,
            "upload_available must be false when the real draft job "
            "processor's start() failed (no poll loop exists)",
        )

    def test_live_poll_loop_reports_compile_and_upload_available(self):
        """Guardrail leg for C7 (compile side GREEN at base; upload side RED
        until the field exists, GREEN after the fix): a REAL processor with
        a live, iterating poll loop advertises both flags true."""
        from app.services.draft_job_processor import DraftJobProcessor

        processor = build_real_processor(self)
        self._loop, self._thread = start_processor_on_worker_loop(processor)

        first_claim = threading.Event()

        def _claim_none(inner_self):
            first_claim.set()
            return None

        with patch.object(DraftJobProcessor, "_claim_next_job", _claim_none):

            async def _drive():
                await wait_for_first_claim(first_claim, self._loop)

            asyncio.run_coroutine_threadsafe(_drive(), self._loop).result(timeout=30)

        task = processor._task
        self.assertIsNotNone(task, "a successful start must publish a poll-loop task")
        self.assertFalse(
            task.done(),
            "the poll loop must still be alive after its first claim cycle",
        )

        self._mount(processor)
        body = self._capabilities()
        self.assertIs(
            body["compile_available"],
            True,
            "compile_available must be true while the real processor's poll "
            "loop is alive",
        )
        self.assertIn("upload_available", body)
        self.assertIs(
            body["upload_available"],
            True,
            "upload_available must be true while the real processor's poll "
            "loop is alive",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
