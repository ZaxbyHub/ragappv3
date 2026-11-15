"""Watchdog harness for the #1021 residual class (out-of-process bound).

The production wedge (two post-closure master recurrences: runs 42416548773
and 43515466761) starves BOTH pytest-timeout methods — the canary step passes
while the suite silently dies at the step ceiling. The fix is
``scripts/ci_backend_watchdog.py`` wrapping the CI Test step: it watches the
output stream, names every pending test, captures per-process stacks from
outside (py-spy opportunistically; primarily the SIGUSR2 faulthandler file
hook, which dumps without the GIL and to a file pytest capture cannot
swallow), kills the process group, and exits 88.

These arms drive the REAL watchdog script as a subprocess with tiny windows
so the whole file stays fast and deterministic on Linux CI and Windows dev
boxes alike. Platform-specific arms skip with explicit reasons; nothing is
env-gated (unlike the canary file, these wedges are bounded by the watchdog
under test, not by an external flag).
"""

import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WATCHDOG = REPO / "scripts" / "ci_backend_watchdog.py"
HOOK_SOURCE = REPO / "scripts" / "ci_watchdog_faulthandler_hook.py"

IS_POSIX = os.name == "posix"
WEDGE_SLEEP_S = 600
# Real-``python -m pytest`` arms: plugin loading can emit nothing for several
# seconds before the first line (observed >6s on the Windows dev box), so the
# quiet window must exceed startup, not just per-test silence.
PYTEST_ARM_QUIET_S = "15"


def _run_watchdog(args, *, env=None, timeout=90):
    """Run the real watchdog script; returns CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(WATCHDOG), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _probe_alive(pid: int) -> bool:
    if IS_POSIX:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        tail = stat[stat.rfind(")") + 1 :].split()
        return bool(tail) and tail[0] != "Z"
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def _child_marker_cmd(marker: str) -> list[str]:
    return [
        sys.executable,
        "-c",
        textwrap.dedent(
            f"""
            import sys, time
            sys.stdout.write({marker!r})
            sys.stdout.flush()
            time.sleep({WEDGE_SLEEP_S})
            """
        ),
    ]


def test_watchdog_passthrough_on_healthy_child():
    """Transparent wrapper: output streams through and the child's exit code
    is mirrored (the live step log never stalls on the watchdog)."""
    started = time.monotonic()
    proc = _run_watchdog(
        [
            "--skip-preflight",
            "--quiet-window",
            "30",
            "--hard-deadline",
            "120",
            "--",
            sys.executable,
            "-c",
            "import sys, time\n"
            "print('pt_marker_one', flush=True)\n"
            "time.sleep(0.3)\n"
            "print('pt_marker_two', flush=True)\n"
            "sys.exit(3)\n",
        ]
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "pt_marker_one" in proc.stdout
    assert "pt_marker_two" in proc.stdout
    assert time.monotonic() - started < 30


def test_watchdog_fires_on_silent_wedge_names_last_output_and_kills_child():
    """AC-W1 core: a flushed partial line followed by silence fires the
    watchdog — banner, the marker forwarded from the partial (unterminated)
    output, a bounded kill proven by an INDEPENDENT liveness probe."""
    marker = "tests/fake_wedge.py::test_marker ::dispatched-without-newline "
    proc = _run_watchdog(
        [
            "--skip-preflight",
            "--quiet-window",
            "2",
            "--poll-interval",
            "0.3",
            "--hard-deadline",
            "60",
            "--",
            *_child_marker_cmd(marker),
        ]
    )
    assert proc.returncode == 88, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "WATCHDOG-FIRED" in combined
    assert "quiet arm" in combined
    # The flushed partial write reached us live (raw os.read forwarding):
    assert marker.strip()[:40] in proc.stdout
    child_pid = None
    match = re.search(r"child_pid=(\d+)", combined)
    if match:
        child_pid = int(match.group(1))
    assert child_pid, "banner must name the child pid"
    assert not _probe_alive(child_pid), "child must be dead after the fire"


@pytest.mark.skipif(not IS_POSIX, reason="SIGUSR2 faulthandler hook is POSIX-only")
def test_watchdog_stack_dump_arm():
    """AC-W2 direct child: the SIGUSR2 file hook captures the wedged frame
    from OUTSIDE, under the same env wiring as the CI Test step."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="wd_stack_arm_") as tmp:
        hook_dir = Path(tmp) / "hook"
        dump_dir = Path(tmp) / "dumps"
        hook_dir.mkdir()
        dump_dir.mkdir()
        shutil.copy(HOOK_SOURCE, hook_dir / "sitecustomize.py")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(hook_dir)
        env["WATCHDOG_DUMP_DIR"] = str(dump_dir)
        env["PYTHONFAULTHANDLER"] = "1"
        child = [
            sys.executable,
            "-c",
            "def wedge_stack_frame_marker():\n"
            "    import time\n"
            f"    time.sleep({WEDGE_SLEEP_S})\n"
            "wedge_stack_frame_marker()\n",
        ]
        proc = _run_watchdog(
            ["--quiet-window", "2", "--poll-interval", "0.3", "--hard-deadline", "60", "--", *child],
            env=env,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 88, combined
        assert "faulthandler stacks pid" in combined, combined
        assert "wedge_stack_frame_marker" in combined, (
            "the captured dump must name the wedged frame"
        )


@pytest.mark.skipif(not IS_POSIX, reason="provider-denied fallback chain is POSIX-only")
def test_watchdog_stack_dump_provider_denied_fallback_arm(tmp_path):
    """A py-spy that is present but DENIED (exits nonzero) must fall through
    to the SIGUSR2 file capture — denial never blocks the chain."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "py-spy").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "py-spy").chmod(0o755)
    hook_dir = tmp_path / "hook"
    dump_dir = tmp_path / "dumps"
    hook_dir.mkdir()
    dump_dir.mkdir()
    shutil.copy(HOOK_SOURCE, hook_dir / "sitecustomize.py")
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(hook_dir)
    env["WATCHDOG_DUMP_DIR"] = str(dump_dir)
    child = [
        sys.executable,
        "-c",
        "def wedge_stack_frame_marker():\n"
        "    import time\n"
        f"    time.sleep({WEDGE_SLEEP_S})\n"
        "wedge_stack_frame_marker()\n",
    ]
    proc = _run_watchdog(
        ["--quiet-window", "2", "--poll-interval", "0.3", "--hard-deadline", "60", "--", *child],
        env=env,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 88, combined
    assert "faulthandler stacks pid" in combined, (
        "denied py-spy must not block the SIGUSR2 file capture: " + combined
    )
    assert "wedge_stack_frame_marker" in combined


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows pytest-xdist through a pipe emits no controller output "
    "and worker startup exceeds any sane test budget on this platform "
    "(observed: 0 bytes / >45s for two trivial tests); the Linux CI Backend "
    "job runs this arm against real fork-based workers",
)
@pytest.mark.skipif(not IS_POSIX, reason="SIGUSR2 stack capture is POSIX-only")
def test_watchdog_xdist_worker_stack_arm(tmp_path):
    """AC-W2 production-equivalent: a REAL ``pytest -v -n 2`` run under the
    EXACT production capture mode (default capture, no -s) with the same
    committed hook — BOTH wedged workers' frames must reach the watchdog
    output (via the per-PID dump files, which xdist capture cannot swallow)."""
    hook_dir = tmp_path / "hook"
    dump_dir = tmp_path / "dumps"
    hook_dir.mkdir()
    dump_dir.mkdir()
    shutil.copy(HOOK_SOURCE, hook_dir / "sitecustomize.py")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(hook_dir)
    env["WATCHDOG_DUMP_DIR"] = str(dump_dir)
    env["PYTHONFAULTHANDLER"] = "1"
    suite = tmp_path / "test_wedge_workers.py"
    suite.write_text(
        "import time\n\n"
        "def wedge_worker_frame_marker_one():\n"
        f"    time.sleep({WEDGE_SLEEP_S})\n\n"
        "def wedge_worker_frame_marker_two():\n"
        f"    time.sleep({WEDGE_SLEEP_S})\n\n"
        "def test_wedge_one():\n"
        "    wedge_worker_frame_marker_one()\n\n"
        "def test_wedge_two():\n"
        "    wedge_worker_frame_marker_two()\n",
        encoding="utf-8",
    )
    proc = _run_watchdog(
        [
            "--quiet-window",
            PYTEST_ARM_QUIET_S,
            "--poll-interval",
            "0.5",
            "--hard-deadline",
            "90",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "-n",
            "2",
            "--timeout=60",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            str(suite),
        ],
        env=env,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 88, combined
    assert "wedge_worker_frame_marker_one" in combined, (
        "worker one's frame must be captured"
    )
    assert "wedge_worker_frame_marker_two" in combined, (
        "worker two's frame must be captured"
    )


def _load_watchdog_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ci_backend_watchdog_under_test", WATCHDOG
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watchdog_pending_tracker_synthetic_stream():
    """Cross-platform pending-set semantics (plan F10): an id is added when
    DISPATCHED — whether newline-terminated-but-uncompleted (the plan-critic
    round-2 probe shape: ``test_worker_two_marker \\r\\ntest_worker_one_marker ``)
    or as the final unterminated partial — and removed only by a result
    record. Runs everywhere; the real-pytest proof is the xdist arms."""
    mod = _load_watchdog_module()
    tracker = mod.StreamTracker.__new__(mod.StreamTracker)
    tracker._pipe = None
    tracker._lock = __import__("threading").Lock()
    tracker._rolling = bytearray()
    tracker._partial = bytearray()
    from collections import deque

    tracker._completed = deque(maxlen=512)
    tracker.pending = {}
    tracker.last_activity = 0.0
    tracker.reader_error = None
    import threading

    tracker._thread = threading.Thread(target=lambda: None, daemon=True)

    stream = (
        b"============================= test session starts =============================\r\n"
        b"platform linux -- Python 3.11.9\r\n"
        b"tests/test_wedge.py::test_fast_control_marker PASSED [ 33%]\r\n"
        b"tests/test_wedge.py::test_worker_two_marker \r\n"
        b"tests/test_wedge.py::test_worker_one_marker "
    )
    tracker._absorb(stream)
    assert set(tracker.pending) == {
        "tests/test_wedge.py::test_worker_two_marker",
        "tests/test_wedge.py::test_worker_one_marker",
    }, tracker.pending
    # A result record retires exactly its id.
    tracker._absorb(
        b"\r\ntests/test_wedge.py::test_worker_two_marker FAILED [ 66%]\r\n"
    )
    assert set(tracker.pending) == {
        "tests/test_wedge.py::test_worker_one_marker"
    }, tracker.pending
    # An xdist crash report also retires the id (no longer pending).
    tracker._absorb(
        b"tests/test_wedge.py::test_worker_one_marker crashed while running\r\n"
    )
    assert tracker.pending == {}, tracker.pending


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows pytest-xdist through a pipe emits no controller output "
    "(see test_watchdog_xdist_worker_stack_arm); the pending-set semantics "
    "run cross-platform in the synthetic-stream arm below and for real on "
    "Linux CI via this arm",
)
def test_watchdog_pending_naming_real_pytest_xdist(tmp_path):
    """AC-W1 pending-set semantics on a REAL ``pytest -v -n 2`` stream: both
    wedged node ids are pending, the completed control is not."""
    suite = tmp_path / "test_wedge_pending.py"
    suite.write_text(
        "import time\n\n"
        "def test_fast_control_marker():\n"
        "    assert True\n\n"
        "def test_wedge_pending_alpha():\n"
        f"    time.sleep({WEDGE_SLEEP_S})\n\n"
        "def test_wedge_pending_beta():\n"
        f"    time.sleep({WEDGE_SLEEP_S})\n",
        encoding="utf-8",
    )
    proc = _run_watchdog(
        [
            "--skip-preflight",
            "--quiet-window",
            PYTEST_ARM_QUIET_S,
            "--poll-interval",
            "0.5",
            "--hard-deadline",
            "90",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "-n",
            "2",
            "--timeout=60",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            str(suite),
        ],
        timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 88, combined
    assert "WATCHDOG-FIRED" in combined
    # Isolate the pending section of the banner.
    lines = combined.splitlines()
    try:
        start = next(
            i for i, ln in enumerate(lines) if ln.startswith("pending tests (")
        )
    except StopIteration:
        pytest.fail(f"banner has no pending-tests section:\n{combined}")
    pending_block = []
    for ln in lines[start + 1 :]:
        if not ln.startswith("  - "):
            break
        pending_block.append(ln)
    assert len(pending_block) == 2, f"expected 2 pending ids, got {pending_block}"
    joined = "\n".join(pending_block)
    assert "test_wedge_pending_alpha" in joined
    assert "test_wedge_pending_beta" in joined
    assert "test_fast_control_marker" not in joined


@pytest.mark.skipif(not IS_POSIX, reason="process-group kill semantics are POSIX-only")
def test_watchdog_posix_kills_process_group():
    """F4: SIGTERM-to-the-group reaches grandchildren too; both PIDs verified
    dead by independent /proc probes, never by trusting the watchdog's word."""
    child = [
        sys.executable,
        "-c",
        "import os, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import os; print(\"GRANDCHILD_PID\", os.getpid(), flush=True); "
        "import time; time.sleep(600)'])\n"
        "print('CHILD_PID', os.getpid(), flush=True)\n"
        "time.sleep(600)\n",
    ]
    proc = _run_watchdog(
        [
            "--skip-preflight",
            "--quiet-window",
            "2",
            "--poll-interval",
            "0.3",
            "--hard-deadline",
            "60",
            "--",
            *child,
        ]
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 88, combined
    pids = {}
    for line in combined.splitlines():
        if line.startswith(("CHILD_PID", "GRANDCHILD_PID")):
            kind, pid = line.split()
            pids[kind] = int(pid)
    assert set(pids) == {"CHILD_PID", "GRANDCHILD_PID"}, combined
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
        _probe_alive(p) for p in pids.values()
    ):
        time.sleep(0.2)
    for kind, pid in pids.items():
        assert not _probe_alive(pid), f"{kind} ({pid}) survived the group kill"
