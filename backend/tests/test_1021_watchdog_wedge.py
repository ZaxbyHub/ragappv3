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
import threading
import time
from collections import deque
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


def _fresh_tracker(mod):
    """A StreamTracker instance built via __new__ (no reader thread started)
    with every instance attribute the tracker methods touch pre-initialized —
    shared by the synthetic-stream and bounded-matching probes."""
    tracker = mod.StreamTracker.__new__(mod.StreamTracker)
    tracker._pipe = None
    tracker._lock = threading.Lock()
    tracker._partial = bytearray()
    tracker._completed = deque(maxlen=512)
    tracker.pending = {}
    tracker._provisional_id = None
    tracker.last_activity = 0.0
    tracker.forwarding_inflight_since = None
    tracker.reader_error = None
    tracker._thread = threading.Thread(target=lambda: None, daemon=True)
    return tracker


def test_watchdog_pending_tracker_synthetic_stream():
    """Cross-platform pending-set semantics (plan F10): an id is added when
    DISPATCHED — whether newline-terminated-but-uncompleted (the plan-critic
    round-2 probe shape: ``test_worker_two_marker \\r\\ntest_worker_one_marker ``)
    or as the final unterminated partial — and removed only by a result
    record. Runs everywhere; the real-pytest proof is the xdist arms."""
    mod = _load_watchdog_module()

    tracker = _fresh_tracker(mod)

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

    # Implementation-review round 1 shapes: class-path ids are captured in
    # FULL (not truncated at the class component) ...
    tracker2 = _fresh_tracker(mod)
    # The record is newline-TERMINATED (the real -v shape: a dispatched id
    # ends its record when the next event arrives), so it is a completed-line
    # dispatch — not speculative — and survives later absorbs.
    tracker2._absorb(b"tests/test_wedge.py::TestGroup::test_class_case \r\n")
    assert set(tracker2.pending) == {
        "tests/test_wedge.py::TestGroup::test_class_case"
    }, tracker2.pending
    # ... and an id that GROWS across chunk boundaries leaves no phantom
    # prefix when its full form completes (split absorb, prefix then rest).
    tracker2._absorb(b"tests/test_wedge.py::TestGrow")
    assert "tests/test_wedge.py::TestGrow" in tracker2.pending, tracker2.pending
    tracker2._absorb(b"::test_grown PASSED [100%]\r\n")
    assert "tests/test_wedge.py::TestGrow" not in tracker2.pending, (
        f"phantom prefix pending after growth completion: {tracker2.pending}"
    )
    # The class-path id from the earlier dispatch is deliberately never
    # completed in this stream: it stays pending, captured in full.
    assert set(tracker2.pending) == {
        "tests/test_wedge.py::TestGroup::test_class_case"
    }, tracker2.pending

    # Implementation-review round 2: a read can split a node id
    # MID-COMPONENT (`tests/x.py::test_fo` + `o PASSED`) — the speculative
    # prefix must be retired when the completed record arrives, and the
    # pending set must end up exactly the genuinely-still-pending ids.
    tracker3 = _fresh_tracker(mod)
    tracker3._absorb(b"tests/test_wedge.py::test_mid_component_fo")
    assert "tests/test_wedge.py::test_mid_component_fo" in tracker3.pending
    tracker3._absorb(b"o PASSED [ 90%]\r\ntests/test_wedge.py::test_still_pending ")
    assert "tests/test_wedge.py::test_mid_component_foo" not in tracker3.pending
    assert "tests/test_wedge.py::test_mid_component_fo" not in tracker3.pending, (
        f"mid-component split left a phantom prefix: {tracker3.pending}"
    )
    assert set(tracker3.pending) == {
        "tests/test_wedge.py::test_still_pending"
    }, tracker3.pending


def test_watchdog_pending_tracker_long_py_line_is_bounded():
    """micro-uib-001 residual (bounded matching): a long `.py`-bearing line
    that cannot yield a node id must fail LINEARLY, not quadratically, under
    the tracker lock (the pre-possessive regex re-scanned the whole run from
    every start position). Oracle-based probe: the module regex must beat an
    in-test UNBOUNDED legacy pattern on the SAME line by a wide factor —
    robust to CI machine speed, unlike a wall-clock bound (review round 5).
    Plus the control that a genuinely LONG VALID id still tracks."""
    legacy = re.compile(
        r"[\w./\\\-]+\.(?:py|pyi)(?:::[\w./\-]+)+(?:\[[^\]]*\])?"
    )
    mod = _load_watchdog_module()
    tracker = _fresh_tracker(mod)
    long_line = ("tests/" + "a" * 50_000 + ".py no components here\n").encode()
    legacy_started = time.monotonic()
    [m.group(0) for m in legacy.finditer(long_line.decode())]
    legacy_s = time.monotonic() - legacy_started
    module_started = time.monotonic()
    tracker._absorb(long_line)
    module_s = time.monotonic() - module_started
    assert tracker.pending == {}, tracker.pending
    assert module_s < legacy_s / 2, (
        f"module matching took {module_s:.2f}s vs legacy {legacy_s:.2f}s — "
        "quadratic backtracking is back"
    )
    # Control: a legitimately LONG VALID node id still parses and tracks.
    long_valid = _fresh_tracker(mod)
    long_valid._absorb(("tests/" + "b" * 50_000 + ".py::test_long_valid ").encode())
    assert "test_long_valid" in "".join(long_valid.pending), (
        f"bounded matching lost a long VALID id: {long_valid.pending}"
    )


def test_watchdog_pending_tracker_unclosed_bracket_is_bounded():
    """review rounds 4+5: MANY unclosed-bracket candidates on one line must
    not go quadratic either — with the uncapped optional bracket, every
    candidate scanned the whole remaining suffix. Oracle-based probe: the
    module regex must beat an in-test UNCAPPED pattern on the SAME line by a
    wide factor (robust to CI machine speed), and the bounded window must
    keep distinct full identities for params within it (a 300-char param
    completes only its OWN dispatch key)."""
    uncapped = re.compile(
        r"(?:\A|\s)(?P<node>[\w./\\\-]++(?:(?<=\.pyi)|(?<=\.py))"
        r"(?:::[\w./\\\-]++)++(?:\[[^\]]*\])?)"
    )
    mod = _load_watchdog_module()
    line = " ".join(["tests/a.py::test_x["] * 20_000)
    uncapped_started = time.monotonic()
    [m.group(0) for m in uncapped.finditer(line)]
    uncapped_s = time.monotonic() - uncapped_started
    tracker = _fresh_tracker(mod)
    module_started = time.monotonic()
    tracker._absorb(line.encode())
    module_s = time.monotonic() - module_started
    assert module_s < uncapped_s / 2, (
        f"module matching took {module_s:.2f}s vs uncapped {uncapped_s:.2f}s "
        "— the suffix-wide bracket scan is back"
    )
    # An unclosed `[` on the (pathological single-line) stream extends the
    # one tracked key to end-of-line by depth resolution — exactly one
    # pending key, prefixed by the candidate id.
    assert len(tracker.pending) == 1, tracker.pending
    (tracked,) = tracker.pending
    assert tracked.startswith("tests/a.py::test_x["), tracked[:60]
    # Identity control: two long (300-char) params of the same function keep
    # DISTINCT keys inside the 2048 window; completing one leaves the other.
    param_tracker = _fresh_tracker(mod)
    param_tracker._absorb(
        ("tests/p.py::t[" + "1" * 300 + "] \n").encode()
        + ("tests/p.py::t[" + "2" * 300 + "] \n").encode()
    )
    keys = sorted(param_tracker.pending)
    assert len(keys) == 2 and keys[0] != keys[1], (
        f"long params within the window must keep distinct keys: {keys}"
    )
    param_tracker._absorb(("tests/p.py::t[" + "1" * 300 + "] PASSED\n").encode())
    remaining = sorted(param_tracker.pending)
    assert remaining == ["tests/p.py::t[" + "2" * 300 + "]"], (
        f"completing one long param must retire only its own key: {remaining}"
    )


def test_watchdog_pending_tracker_params_beyond_any_limit_keep_identity():
    """review rounds 4-6 (full identity, no cap): param ids of ANY length
    keep distinct pending keys — the tracked id's bracket is reattached
    after the bounded base match with one str.find, so there is NO length
    limit to move. Two concurrently-dispatched parameterizations with
    3000-char params get two distinct keys; completing one retires only its
    own and the other stays pending (the banner contract 'names EVERY
    pending test' holds at every param length)."""
    mod = _load_watchdog_module()
    tracker = _fresh_tracker(mod)
    tracker._absorb(
        ("tests/p.py::t[" + "1" * 3000 + "] \n").encode()
        + ("tests/p.py::t[" + "2" * 3000 + "] \n").encode()
    )
    keys = sorted(tracker.pending)
    assert len(keys) == 2 and keys[0] != keys[1], (
        f"params beyond 2048 chars must keep distinct keys: {len(keys)} keys"
    )
    tracker._absorb(("tests/p.py::t[" + "1" * 3000 + "] PASSED\n").encode())
    remaining = sorted(tracker.pending)
    assert remaining == ["tests/p.py::t[" + "2" * 3000 + "]"], (
        f"completing one must retire only its own key: {remaining}"
    )


def test_watchdog_rejects_abbreviated_options():
    """review round 6: argparse long-option abbreviation is DISABLED
    (allow_abbrev=False) — an abbreviated duplicate-override such as
    `--poll-i=50` must be REJECTED (SystemExit 2), not silently accepted as
    an override the wiring pin can't see."""
    mod = _load_watchdog_module()
    with pytest.raises(SystemExit) as ei:
        mod.parse_args(["--poll-i=50", "--", "true"])
    assert ei.value.code == 2


def test_watchdog_pending_tracker_node_like_text_inside_param_bracket():
    """review rounds 6-8: node-like text INSIDE a param bracket is not a
    test — bracket-depth-aware candidate selection keeps the OUTER
    parameterized id as the tracked key, and depth resolution recovers the
    FULL id (spaces and all) through dispatch and completion."""
    mod = _load_watchdog_module()
    tracker = _fresh_tracker(mod)
    tracker._absorb(b"tests/p.py::t[input tests/other.py::test_x] \n")
    assert set(tracker.pending) == {
        "tests/p.py::t[input tests/other.py::test_x]"
    }, tracker.pending
    tracker._absorb(
        b"tests/p.py::t[input tests/other.py::test_x] PASSED [100%]\n"
    )
    assert tracker.pending == {}, tracker.pending


def test_watchdog_pending_tracker_space_param_ids_keep_identity():
    """review round 8: explicit param ids containing SPACES keep distinct
    pending keys — depth resolution extends a whitespace token to its
    closing bracket, so `t[input one]` and `t[input two]` never collide;
    completing one retires only its own key."""
    mod = _load_watchdog_module()
    tracker = _fresh_tracker(mod)
    tracker._absorb(
        b"tests/p.py::t[input one] \ntests/p.py::t[input two] \n"
    )
    assert set(tracker.pending) == {
        "tests/p.py::t[input one]",
        "tests/p.py::t[input two]",
    }, tracker.pending
    tracker._absorb(b"tests/p.py::t[input one] PASSED [ 50%]\n")
    assert set(tracker.pending) == {"tests/p.py::t[input two]"}, (
        tracker.pending
    )


def test_watchdog_pending_tracker_close_bracket_and_space_combined():
    """review round 8: explicit param ids containing BOTH an embedded `]`
    and spaces (`[a] one]` vs `[a] two]` — depth-to-zero ends both after
    `[a]`) keep DISTINCT keys — the id ends at the LAST `]` followed by
    whitespace/EOL that does not close a progress bracket; completing one
    retires only its own key."""
    mod = _load_watchdog_module()
    tracker = _fresh_tracker(mod)
    tracker._absorb(
        b"tests/p.py::t[a] one] \ntests/p.py::t[a] two] \n"
    )
    assert set(tracker.pending) == {
        "tests/p.py::t[a] one]",
        "tests/p.py::t[a] two]",
    }, tracker.pending
    tracker._absorb(b"tests/p.py::t[a] one] PASSED [ 50%]\n")
    assert set(tracker.pending) == {"tests/p.py::t[a] two]"}, (
        tracker.pending
    )


def test_watchdog_pending_tracker_xdist_completion_prefix_shape():
    """CI round 2 regression, CROSS-PLATFORM: xdist completion records render
    as ``[gwN] [ NN%] OUTCOME <id>`` — the id is preceded by WHITESPACE, and
    resolution must slice from the node GROUP start, not the whole-match
    start (which includes the consumed ``\\s`` anchor), or completion keys
    gain a leading space and never retire the dispatch keys. The anchor-
    slice bug shipped on cd7982cd and was caught only by the Linux real-
    stream regression (skipped on Windows); this synthetic shape pins it
    everywhere."""
    mod = _load_watchdog_module()
    tracker = _fresh_tracker(mod)
    tracker._absorb(b"tests/x.py::t[a b] \n")
    assert set(tracker.pending) == {"tests/x.py::t[a b]"}, tracker.pending
    tracker._absorb(b"[gw0] [ 50%] PASSED tests/x.py::t[a b]\n")
    assert tracker.pending == {}, tracker.pending


def test_watchdog_tracker_on_real_pytest_param_stream(tmp_path):
    """review round 9 — ground truth from a REAL pytest -v stream: explicit
    ids containing `]`, spaces, and both (`a] one` / `b two]`) render as
    ``id OUTCOME [ NN%]`` with the progress bracket at line end. The REAL
    captured stream is the falsifier the review demanded: the tracker must
    retire BOTH ids on the full stream (nothing stuck pending), and a
    wedge-simulated truncation before the last outcome must leave the final
    id pending with its FULL identity, retired exactly by the completion
    record."""
    mod = _load_watchdog_module()
    suite = tmp_path / "test_param_ids_probe.py"
    suite.write_text(
        "import pytest\n\n"
        '@pytest.mark.parametrize("x", [1, 2], ids=["a] one", "b two]"])\n'
        "def test_t(x):\n"
        "    assert x\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            str(suite),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        # Outside the repo tree: the backend conftest imports the app and
        # would turn this two-test probe into a multi-minute run.
        cwd=str(tmp_path),
    )
    chunks: list[bytes] = []
    fd = proc.stdout.fileno()
    while True:
        # 32-byte reads: the tiny probe stream would otherwise arrive as ONE
        # production-size chunk; forcing small reads deterministically
        # produces the mid-record split boundaries that the multi-MB
        # production stream hits stochastically (production reads 64KB
        # chunks whose boundaries land anywhere).
        chunk = os.read(fd, 32)
        if not chunk:
            break
        chunks.append(chunk)
    proc.wait(timeout=120)
    assert proc.returncode == 0, b"".join(chunks)
    # Leg 1 — the full stream replayed through the tracker at the OBSERVED
    # split-prone os.read chunk boundaries, recording the pending state
    # AFTER EVERY chunk so the intermediate DISPATCH state is proven, not
    # just final emptiness.
    tracker = _fresh_tracker(mod)
    ever_pending: set[str] = set()
    for chunk in chunks:
        tracker._absorb(chunk)
        ever_pending |= set(tracker.pending)
    assert tracker.pending == {}, tracker.pending
    assert any(
        k.endswith("a] one]") or k.endswith("b two]]") for k in ever_pending
    ), (
        "at some observed split boundary the FULL pathological id must "
        f"have been the live pending state: {sorted(ever_pending)}"
    )
    # Leg 2 — a SYNTHETIC split (explicitly labeled): truncate before the
    # LAST outcome token to simulate a wedge mid-record.
    stream = b"".join(chunks)
    outcome_idx = stream.rfind(b"PASSED")
    assert outcome_idx != -1
    tracker2 = _fresh_tracker(mod)
    tracker2._absorb(stream[:outcome_idx])
    assert len(tracker2.pending) == 1, tracker2.pending
    (key,) = tracker2.pending
    assert "test_t[" in key and (
        key.endswith("a] one]") or key.endswith("b two]]")
    ), f"the pending key must keep the FULL pathological id: {key!r}"
    tracker2._absorb(stream[outcome_idx:])
    assert tracker2.pending == {}, tracker2.pending


@pytest.mark.skipif(
    not IS_POSIX, reason="real xdist through a pipe is POSIX-only in this suite"
)
def test_watchdog_tracker_on_real_xdist_param_stream(tmp_path):
    """review round 10 — PRIMARY xdist evidence: the captured REAL
    ``pytest -v -n 2`` stream for the pathological explicit ids is the
    production-shaped falsifier the review demanded. The tracker must
    retire BOTH ids on the full stream, and a wedge-simulated truncation
    before the last outcome must leave the final id pending with its FULL
    identity. This pins the terminal-close rule against the REAL xdist
    record framing — whatever that framing is, it is captured here, not
    assumed."""
    mod = _load_watchdog_module()
    suite = tmp_path / "test_param_ids_probe_xdist.py"
    suite.write_text(
        "import pytest\n\n"
        '@pytest.mark.parametrize("x", [1, 2], ids=["a] one", "b two]"])\n'
        "def test_t(x):\n"
        "    assert x\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "-n",
            "2",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            str(suite),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        cwd=str(tmp_path),
    )
    chunks: list[bytes] = []
    fd = proc.stdout.fileno()
    while True:
        # 32-byte reads: force the mid-record split boundaries (see the
        # serial probe for the full rationale).
        chunk = os.read(fd, 32)
        if not chunk:
            break
        chunks.append(chunk)
    proc.wait(timeout=120)
    assert proc.returncode == 0, b"".join(chunks)
    # Leg 1 — full stream replayed at the OBSERVED split-prone os.read
    # chunk boundaries, recording the pending state AFTER EVERY chunk so
    # the intermediate DISPATCH state is proven, not just final emptiness.
    tracker = _fresh_tracker(mod)
    ever_pending: set[str] = set()
    for chunk in chunks:
        tracker._absorb(chunk)
        ever_pending |= set(tracker.pending)
    assert tracker.pending == {}, tracker.pending
    assert any(
        k.endswith("a] one]") or k.endswith("b two]]") for k in ever_pending
    ), (
        "at some observed split boundary the FULL pathological id must "
        f"have been the live pending state: {sorted(ever_pending)}"
    )
    # Leg 2 — a SYNTHETIC split (explicitly labeled): truncate before the
    # LAST outcome token to simulate a wedge mid-record.
    stream = b"".join(chunks)
    outcome_idx = stream.rfind(b"PASSED")
    assert outcome_idx != -1
    tracker2 = _fresh_tracker(mod)
    tracker2._absorb(stream[:outcome_idx])
    assert len(tracker2.pending) == 1, tracker2.pending
    (key,) = tracker2.pending
    assert "test_t[" in key and (
        key.endswith("a] one]") or key.endswith("b two]]")
    ), f"the pending key must keep the FULL pathological id: {key!r}"
    tracker2._absorb(stream[outcome_idx:])
    assert tracker2.pending == {}, tracker2.pending


def test_watchdog_pending_tracker_close_bracket_inside_param_keeps_identity():
    """review round 7: explicit param ids containing `]` (distinct pytest
    node ids like `[a]one` and `[a]two`) keep DISTINCT pending keys — the
    tracked token ends at the next WHITESPACE, not at the first `]`;
    completing one retires only its own key."""
    mod = _load_watchdog_module()
    tracker = _fresh_tracker(mod)
    tracker._absorb(b"tests/p.py::t[a]one \ntests/p.py::t[a]two \n")
    assert set(tracker.pending) == {
        "tests/p.py::t[a]one",
        "tests/p.py::t[a]two",
    }, tracker.pending
    tracker._absorb(b"tests/p.py::t[a]one PASSED [ 50%]\n")
    assert set(tracker.pending) == {"tests/p.py::t[a]two"}, tracker.pending


def test_watchdog_fires_deadline_arm_before_quiet_window():
    """tests-falsifiability-004: the hard-deadline arm is independently
    reachable. A silent child with quiet-window > hard-deadline must fire the
    DEADLINE arm (never the quiet arm) with the bounded kill."""
    marker = "tests/fake_deadline.py::test_marker deadline-silence "
    proc = _run_watchdog(
        [
            "--skip-preflight",
            "--quiet-window",
            "60",
            "--poll-interval",
            "0.3",
            "--hard-deadline",
            "2",
            "--",
            *_child_marker_cmd(marker),
        ]
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 88, combined
    assert "WATCHDOG-FIRED" in combined
    assert "deadline arm" in combined
    assert "quiet arm" not in combined


@pytest.mark.skipif(not IS_POSIX, reason="fail-closed hook preflight is POSIX-only")
def test_watchdog_preflight_refuses_without_dump_dir(tmp_path):
    """tests-falsifiability-005 / plan F18 fail-closed: with the hook
    preflight UNSKIPPED and WATCHDOG_DUMP_DIR unset, the watchdog refuses
    (exit 3) BEFORE spawning the suite — proven by the child's side effect
    never appearing (the suite-never-ran sentinel stays absent)."""
    sentinel = tmp_path / "suite-never-ran"
    env = os.environ.copy()
    env.pop("WATCHDOG_DUMP_DIR", None)
    proc = _run_watchdog(
        [
            "--quiet-window",
            "60",
            "--hard-deadline",
            "60",
            "--",
            sys.executable,
            "-c",
            "import pathlib, time\n"
            f"pathlib.Path({str(sentinel)!r}).touch()\n"
            "print('suite ran', flush=True)\n"
            "time.sleep(600)\n",
        ],
        env=env,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 3, combined
    assert "WATCHDOG_DUMP_DIR is not set" in combined
    assert "refusing to run the suite unmonitored" in combined
    assert not sentinel.exists(), (
        "the suite must NEVER start on a preflight refusal — the child ran"
    )


def test_watchdog_run_fires_forwarding_arm_on_stalled_forwarding(capsys):
    """reliability-performance-001: a forwarding write in flight past the
    quiet window fires the FORWARDING arm — checked BEFORE the quiet arm so a
    wedged log drain is never misattributed to child silence. In-process
    probe: a stub tracker reports a stalled write while a REAL child sleeps;
    run() must fire the forwarding arm and bound-kill the child."""
    import types

    mod = _load_watchdog_module()

    class _StalledTracker:
        def __init__(self, pipe):
            self.reader_error = None

        def start(self):
            pass

        def snapshot(self):
            self.snapshot_called = True
            return (999.0, 999.0)

        def pending_ids(self):
            return []

        def tail_lines(self, count):
            return []

        def drain(self, timeout_s=2.0):
            pass

        def forwarding_stall_for(self):
            # Display use after the fire is fine; ARM SELECTION must consult
            # snapshot() (round 4 Important-1: one consistent sample).
            if not getattr(self, "snapshot_called", False):
                raise AssertionError(
                    "arm selection must sample quiet+stall from ONE "
                    "snapshot() before any separate accessor call"
                )
            return 999.0

        def quiet_for(self):
            if not getattr(self, "snapshot_called", False):
                raise AssertionError(
                    "arm selection must sample quiet+stall from ONE "
                    "snapshot() before any separate accessor call"
                )
            return 999.0

    real_tracker = mod.StreamTracker
    mod.StreamTracker = _StalledTracker
    try:
        args = types.SimpleNamespace(
            skip_preflight=True,
            command=[sys.executable, "-c", "import time; time.sleep(600)"],
            quiet_window=60.0,
            poll_interval=0.3,
            hard_deadline=600.0,
            dump_tail=5,
            dump_budget=5.0,
        )
        code = mod.run(args)
    finally:
        mod.StreamTracker = real_tracker
    assert code == 88
    err = capsys.readouterr().err
    assert "forwarding arm" in err, err
    assert "quiet arm" not in err, err


def test_tracker_stamps_forwarding_inflight_around_the_write():
    """reliability-performance-001 (stamp truth): the in-flight stamp goes
    down BEFORE the forwarding write and is observable WHILE the write runs
    (a stalled drain is visible mid-write, not only in hindsight), then is
    cleared after the write completes — never left set."""
    mod = _load_watchdog_module()
    read_fd, write_fd = os.pipe()
    pipe = open(read_fd, "rb", buffering=0)
    tracker = mod.StreamTracker(pipe)
    observed = {}

    class _SinkBuffer:
        @staticmethod
        def write(data):
            observed["during_write"] = (
                tracker.forwarding_inflight_since is not None
            )
            return len(data)

        @staticmethod
        def flush():
            pass

    class _SinkStdout:
        buffer = _SinkBuffer

    real_stdout = sys.stdout
    sys.stdout = _SinkStdout()
    try:
        tracker.start()
        os.write(write_fd, b"hello\n")
        os.close(write_fd)  # EOF: the reader loop returns after the absorb
        tracker._thread.join(timeout=5.0)
    finally:
        sys.stdout = real_stdout
        pipe.close()
    assert observed.get("during_write") is True, (
        "the in-flight stamp was not set at write time (stamp runs before "
        "the forwarding write)"
    )
    assert tracker.forwarding_inflight_since is None, (
        "the in-flight stamp was left set after a completed write"
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


@pytest.mark.skipif(
    not IS_POSIX,
    reason="TERM-resistant descendant kill is a POSIX process-group behavior",
)
def test_watchdog_posix_kills_term_resistant_descendant():
    """Implementation-review round 1: the controller may exit on SIGTERM while
    a descendant IGNORES SIGTERM — the unconditional bounded SIGKILL-to-the-
    group must still reap it (the kill may never be gated on direct-child
    liveness)."""
    child = [
        sys.executable,
        "-c",
        "import os, signal, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "import os; print(\"RESISTANT_PID\", os.getpid(), flush=True); "
        "time.sleep(600)'])\n"
        "print('TERM_CONTROLLER_PID', os.getpid(), flush=True)\n"
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
        if line.startswith(("TERM_CONTROLLER_PID", "RESISTANT_PID")):
            kind, pid = line.split()
            pids[kind] = int(pid)
    assert set(pids) == {"TERM_CONTROLLER_PID", "RESISTANT_PID"}, combined
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
        _probe_alive(p) for p in pids.values()
    ):
        time.sleep(0.2)
    for kind, pid in pids.items():
        assert not _probe_alive(pid), (
            f"{kind} ({pid}) survived the group SIGKILL (TERM-resistant "
            "descendant must die with the group)"
        )
