"""Companion tests for the #1218 review round (the findings the swarm PR
review raised against the shutdown/lifecycle work).

Each test pins ONE property that a plausible regression would break:

- the post-commit audit drain registers the critical-work gate, so the
  bounded tail exit cannot fire while a security-audit row is being written
  (the row is never regenerated);
- the cooperative curator abort fires for an orchestration that is ALREADY
  running (the flag is checked inside the semaphore, not before it);
- ``BackgroundProcessor.stop()`` reaches its terminal state even when the
  caller cancels it from outside;
- the shipped Windows sender reports its exit status instead of failing
  silently, and the grouped launcher writes the marker format the stop
  script parses.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))


class TestAuditDrainIsRegistered(unittest.IsolatedAsyncioTestCase):
    """The audit drain holds the critical-work gate while it writes."""

    async def test_registry_is_held_during_the_audit_write(self):
        from app.services import shutdown_gate, task_drain

        observed = {}

        async def _fake_drain_await(awaitable):
            # The audit task is live here: that is the window in which the
            # bounded tail exit must NOT fire.
            observed["during"] = shutdown_gate.critical_work_active()
            return False, None

        async def _audit():
            return None

        with mock.patch.object(task_drain, "drain_await", _fake_drain_await):
            await task_drain.drain_audit(_audit())

        self.assertTrue(
            observed.get("during"),
            "the audit drain must hold the critical-work gate while the "
            "security-audit row is written, or the bounded tail exit can "
            "end the process with the row unwritten",
        )
        self.assertFalse(
            shutdown_gate.critical_work_active(),
            "the gate must be released when the audit drain finishes",
        )


class TestCuratorAbortFiresForInFlightWork(unittest.IsolatedAsyncioTestCase):
    """A chunk that reaches the front of the queue AFTER the flag is set
    skips its propose call."""

    async def test_later_chunks_skip_propose_after_the_flag_is_set(self):
        from app.services import wiki_compiler, wiki_curator

        calls = []

        class _Client:
            async def propose(self, messages):
                calls.append(messages)
                # The first propose blocks long enough for the flag to be
                # set while the later chunks wait on the semaphore.
                await asyncio.sleep(0.05)
                return '{"quote": "q", "chunk_id": "c"}'

        curator = wiki_curator.WikiCurator(_Client())
        chunks = [
            wiki_curator.CuratorChunk(chunk_id=f"c{i}", source_text="t" * 20)
            for i in range(3)
        ]

        async def _set_flag_soon():
            await asyncio.sleep(0.01)
            wiki_compiler.CURATOR_SHUTDOWN.set()

        try:
            await asyncio.gather(
                curator.curate(vault_id=1, file_id=1, chunks=chunks),
                _set_flag_soon(),
            )
        finally:
            wiki_compiler.CURATOR_SHUTDOWN.clear()

        self.assertLess(
            len(calls),
            len(chunks),
            "the cooperative shutdown flag must skip the chunks that reach "
            "the semaphore AFTER it is set (a check made before the "
            "semaphore is passed by every chunk, so the abort never fires)",
        )


class TestStopReachesTerminalStateWhenCancelled(unittest.IsolatedAsyncioTestCase):
    """An outside cancellation must not leave the processor half-stopped."""

    async def test_terminal_state_survives_an_outside_cancellation(self):
        from app.services.background_tasks import BackgroundProcessor

        processor = BackgroundProcessor.__new__(BackgroundProcessor)
        processor._running = True
        processor._write_semaphore = asyncio.Semaphore(1)
        processor.processor = mock.MagicMock()
        processor.processor.pool.write_permit = object()
        processor._recovery_file_ids = {"a"}
        processor._queued_file_ids = {"b"}
        processor._reindex_job_ids = {"c"}

        # A stage that never finishes: the shape in which an outside
        # cancellation lands inside the shutdown sequence.
        async def _never_finishes():
            await asyncio.sleep(3600)

        stop_task = asyncio.create_task(
            processor._stop_guarded(_never_finishes())
        )
        await asyncio.sleep(0.05)
        stop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stop_task

        self.assertFalse(
            processor._running,
            "an outside cancellation must not leave the processor running: "
            "its worker threads would outlive the shutdown envelope",
        )
        self.assertIsNone(
            processor.processor.pool.write_permit,
            "the write permit must be released even on the cancellation path",
        )
        self.assertEqual(
            processor._queued_file_ids,
            set(),
            "the ownership registries must be cleared even on the "
            "cancellation path",
        )


class TestDeclinedTailLogNamesTheBlockedWorkflow(unittest.IsolatedAsyncioTestCase):
    """The declined-tail log must say the bounded exit was SKIPPED and name
    the recovery-critical workflow that blocked it."""

    async def test_log_names_the_critical_work_and_the_skip(self):
        import logging

        from app.services import shutdown_gate

        # The gate logs through the ROOT logger.
        with self.assertLogs(level="INFO") as captured:
            async with shutdown_gate.critical_shutdown_work("unit-blocker"):
                from app.lifespan import _bounded_tail_exit

                with unittest.mock.patch.dict(
                    os.environ, {"RAGAPP_BOUNDED_EXIT": "1"}
                ):
                    fired = _bounded_tail_exit(True, True)
        self.assertFalse(fired, "the gate must decline while work is registered")
        joined = "\n".join(captured.output)
        self.assertIn(
            "SKIPPED",
            joined,
            "the log must say the bounded tail exit was skipped, not that the "
            "process took a normal exit",
        )
        self.assertIn(
            "unit-blocker",
            joined,
            "the log must name the recovery-critical workflow that blocked "
            "the exit, so an operator triaging a force-cut knows which one",
        )
        self.assertIn(
            "force",
            joined,
            "the log must say the outer force boundary may terminate the "
            "process",
        )



@contextlib.contextmanager
def _sender_with_fake_kernel32(*, attach_ok: bool, event_ok: bool):
    """Import the Windows sender with a fake kernel32 (Linux-runnable).

    The fakes stay installed for the whole ``with`` block: the sender calls
    ``ctypes.get_last_error()`` INSIDE ``main()`` (the failure paths), so
    restoring the names right after the import would leave the call without
    the attribute it needs -- and on Linux that attribute does not exist at
    all.
    """
    import ctypes
    import importlib.util

    class _FakeKernel32:
        def __init__(self):
            self.calls = []

        def FreeConsole(self):
            self.calls.append("FreeConsole")
            return 1

        def AttachConsole(self, pid):
            self.calls.append(f"AttachConsole({pid})")
            return 1 if attach_ok else 0

        def GenerateConsoleCtrlEvent(self, event, group):
            self.calls.append(f"GenerateConsoleCtrlEvent({event},{group})")
            return 1 if event_ok else 0

    fake = _FakeKernel32()
    # `ctypes.WinDLL` and `ctypes.get_last_error` are WINDOWS-ONLY
    # attributes: reading them on Linux raises AttributeError, so both are
    # installed and restored through getattr (never a direct read).
    sentinel = object()
    original_windll = getattr(ctypes, "WinDLL", sentinel)
    original_get_last_error = getattr(ctypes, "get_last_error", sentinel)
    ctypes.WinDLL = lambda *args, **kwargs: fake
    if original_get_last_error is sentinel:
        ctypes.get_last_error = lambda: 0
    try:
        spec = importlib.util.spec_from_file_location(
            "win_send_ctrl_break_under_test",
            REPO / "scripts" / "win_send_ctrl_break.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        if original_windll is sentinel:
            try:
                del ctypes.WinDLL
            except AttributeError:
                pass
        else:
            ctypes.WinDLL = original_windll
        if original_get_last_error is sentinel:
            try:
                del ctypes.get_last_error
            except AttributeError:
                pass
        else:
            ctypes.get_last_error = original_get_last_error


class TestWindowsSenderAndMarker(unittest.TestCase):
    """The shipped Windows helpers report their outcome and agree on the
    marker format (both are Linux-runnable)."""

    def test_sender_reports_its_exit_status(self):
        """The sender's exit statuses are observable without a Windows box.

        The script binds kernel32 at import time, so a Linux run cannot
        even load it; the fake kernel32 makes the four outcomes
        (usage / attach failed / event failed / delivered) testable
        anywhere.
        """
        with _sender_with_fake_kernel32(
            attach_ok=True, event_ok=True
        ) as module:
            self.assertEqual(
                module.main(),
                2,
                "no pid argument must be a usage error (exit 2)",
            )

        # main() reads the pid from argv.
        argv = sys.argv
        try:
            sys.argv = ["win_send_ctrl_break.py", "4242"]
            with _sender_with_fake_kernel32(
                attach_ok=True, event_ok=True
            ) as module:
                self.assertEqual(
                    module.main(),
                    0,
                    "a delivered CTRL_BREAK must report success (exit 0)",
                )

            with _sender_with_fake_kernel32(
                attach_ok=False, event_ok=True
            ) as module:
                self.assertEqual(
                    module.main(),
                    3,
                    "a failed AttachConsole must be reported (exit 3), not silent",
                )

            with _sender_with_fake_kernel32(
                attach_ok=True, event_ok=False
            ) as module:
                self.assertEqual(
                    module.main(),
                    4,
                    "a failed GenerateConsoleCtrlEvent must be reported (exit 4), "
                    "not silent",
                )
        finally:
            sys.argv = argv

    def test_sender_uses_the_windows_api_and_reports_failures(self):
        script = REPO / "scripts" / "win_send_ctrl_break.py"
        source = script.read_text(encoding="utf-8")
        self.assertIn(
            "AttachConsole", source, "the sender must attach to the console"
        )
        self.assertIn(
            "GenerateConsoleCtrlEvent",
            source,
            "the sender must deliver the CTRL_BREAK to the group",
        )
        # The failure paths must be distinguishable from success.
        for marker in ("return 3", "return 4", "return 0"):
            self.assertIn(
                marker,
                source,
                f"the sender must expose {marker} so a failed attach or "
                "event delivery is distinguishable from success",
            )

    def test_marker_line_format_matches_the_stop_parser(self):
        wrapper = (REPO / "scripts" / "win_grouped_backend.py").read_text(
            encoding="utf-8"
        )
        stop = (REPO / "stop-services.ps1").read_text(encoding="utf-8")
        self.assertIn(
            'fh.write("%d %d\\n" % (child.pid, start_ticks))',
            wrapper,
            "the marker line must stay '<pid> <filetime>'",
        )
        self.assertIn(
            "-split '\\s+'",
            stop,
            "the stop script parses the marker as whitespace-separated fields",
        )
        self.assertIn(
            "$markedPid = [int]$parts[0]",
            stop,
            "the stop script reads the pid from the first field",
        )
        self.assertIn(
            "$markedTicks = [math]::Floor([double]$parts[1])",
            stop,
            "the stop script reads the filetime from the second field",
        )

    def test_marker_dir_defaults_to_the_repository_root(self):
        wrapper = (REPO / "scripts" / "win_grouped_backend.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "default_marker_dir",
            wrapper,
            "the marker dir must default to the repository root derived from "
            "the script's own location, not the current working directory "
            "(the stop path always reads <repo-root>/.cache/backend.pid)",
        )
        self.assertNotIn(
            'os.environ.get("RAGAPP_MARKER_DIR", ".")',
            wrapper,
            "the CWD-relative default must be gone",
        )

    def test_path_valued_arguments_are_quoted_in_the_launchers(self):
        for name in ("start-services.ps1", "stop-services.ps1"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "-ArgumentList ($Sender, ",
                text,
                f"{name}: the sender path is path-valued and Windows "
                "PowerShell 5.1 splits an unquoted -ArgumentList element "
                "on spaces",
            )
            self.assertIn(
                "'\"{0}\"' -f $Sender",
                text,
                f"{name}: the sender path must carry embedded quotes",
            )


if __name__ == "__main__":
    unittest.main()
