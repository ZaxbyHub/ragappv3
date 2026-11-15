#!/usr/bin/env python3
"""Out-of-process watchdog for the Backend CI Test step (issue #1021 residual).

Why this exists: the #1021 hang class starves BOTH pytest-timeout methods
(the signal method needs the interpreter loop; the thread method's timeout
thread needs the GIL), so a single wedged test can silently consume the
step's ``timeout-minutes`` ceiling with zero FAILED lines. This wrapper runs
OUTSIDE the pytest process: it watches the output stream for progress, and
when the run goes quiet (or approaches the hard deadline) it

* prints a ``WATCHDOG-FIRED`` banner naming the arm, the elapsed time, and
  every still-pending pytest node id (dispatched, never completed);
* captures per-process Python stacks into per-PID files via a SIGUSR2
  faulthandler hook (registered by ``scripts/ci_watchdog_faulthandler_hook.py``
  through PYTHONPATH sitecustomize — a C-level handler that dumps WITHOUT the
  GIL and to a FILE, so pytest/xdist capture cannot swallow it), with
  py-spy as an opportunistic external provider before it and SIGABRT as the
  fatal last resort;
* terminates the whole process group (bounded) and exits 88 so the step
  fails with a named diagnostic instead of a silent ceiling kill.

Design provenance: issue-tracer trace 1021-1038-ci-wedge-watchdog-parity,
plan-critic rounds 1-5 (approved rev 5). stdlib-only on purpose.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

WATCHDOG_EXIT_CODE = 88
# faulthandler.register is POSIX-only; the hook writes these under
# WATCHDOG_DUMP_DIR. Kept in sync with scripts/ci_watchdog_faulthandler_hook.py.
STACK_FILE_PREFIX = "wd-stacks-"
READY_FILE_PREFIX = "wd-hook-ready-"
READY_MARKER_TEXT = "watchdog-faulthandler-hook-ready"

IS_POSIX = os.name == "posix"

# pytest -v dispatch/result line shapes. A dispatched-but-uncompleted test
# appears as a bare node id (possibly as the final unterminated partial line);
# a completed test's line carries one of the outcome tokens. The id pattern
# spans the FULL node path — file :: Class :: test, with optional [param] —
# so class-path ids are not truncated at the class component, and param
# brackets are included (implementation-review round 1).
_NODE_ID = (
    r"[\w./\\\-]+\.(?:py|pyi)"
    r"(?:::[\w./\-]+)+"  # one or more :: components (module, class(es), test)
    r"(?:\[[^\]]*\])?"
)
_NODE_ID_RE = re.compile(_NODE_ID)
_OUTCOME_RE = re.compile(
    r"\b(?:PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS|CRASHED|crashed while running)\b"
)

_READ_CHUNK = 65536


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command under the Backend CI hang watchdog."
    )
    parser.add_argument(
        "--quiet-window",
        type=float,
        default=360.0,
        help="seconds without output growth before firing (default 360 = the "
        "300s per-test --timeout plus a 60s grace so the in-process timeout "
        "always fires first when it can)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="seconds between monitor polls (default 5)",
    )
    parser.add_argument(
        "--hard-deadline",
        type=float,
        default=2700.0,
        help="wall-clock seconds after which the deadline arm fires "
        "(default 2700 = 45 minutes; FIRE is bounded to ~120s so the step "
        "ceiling still owns the last minutes)",
    )
    parser.add_argument(
        "--dump-tail",
        type=int,
        default=40,
        help="completed output lines echoed as context in the banner",
    )
    parser.add_argument(
        "--dump-budget",
        type=float,
        default=60.0,
        help="total seconds allowed for the whole stack-dump phase",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="internal/test use: skip the POSIX hook readiness preflight",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="after '--': the command to run")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("no command given (pass it after '--')")
    return args


# --- hook readiness preflight (fail closed, plan F18) -----------------------


def _dump_dir() -> Path | None:
    raw = os.environ.get("WATCHDOG_DUMP_DIR")
    return Path(raw) if raw else None


def preflight_hook(skip: bool = False) -> bool:
    """On POSIX, verify the SIGUSR2 faulthandler hook registers in this env.

    Spawns a trivial interpreter with the SAME environment; the hook writes a
    readiness marker on success. Missing marker => False (the caller fails
    the step BEFORE the expensive suite starts — capture must never silently
    degrade). Non-POSIX hosts have no SIGUSR2 hook at all: report None-ish via
    the printed note and return True (preflight is a no-op there).
    """
    if skip or not IS_POSIX:
        if not IS_POSIX:
            eprint("watchdog preflight: file capture is POSIX-only (no-op here)")
        return True
    dump_dir = _dump_dir()
    if dump_dir is None:
        eprint("watchdog preflight: WATCHDOG_DUMP_DIR is not set")
        return False
    # Popen (not subprocess.run): CompletedProcess carries no pid, and the
    # readiness marker is keyed by the PROBE's pid (implementation-review
    # round 1 CRITICAL).
    probe = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    probe_pid = probe.pid
    try:
        probe.wait(timeout=30)
    except subprocess.TimeoutExpired:
        probe.kill()
        probe.wait(timeout=5)
        eprint("watchdog preflight: probe timed out")
        return False
    ready = dump_dir / f"{READY_FILE_PREFIX}{probe_pid}.log"
    # The probe has exited by now; give the filesystem a moment if needed.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.1)
    try:
        text = ready.read_text(encoding="utf-8", errors="replace")
        ready.unlink(missing_ok=True)
    except OSError:
        eprint(f"watchdog preflight: stack-capture hook not ready (no {ready.name})")
        return False
    if READY_MARKER_TEXT not in text:
        eprint("watchdog preflight: stack-capture hook marker text invalid")
        return False
    eprint("watchdog preflight: stack-capture hook ready")
    return True


# --- stream tracking (plan F2/F10/F11) ---------------------------------------


class StreamTracker:
    """Raw-chunk reader + pending-node-id tracker.

    A daemon thread reads the merged child stream with os.read on the raw fd
    (bufsize=0 on the Popen — default buffering withholds flushed partial
    writes until exit), forwards each chunk immediately, and appends to the
    rolling buffer. The pending set tracks dispatched-but-not-completed
    pytest node ids: an id is added the moment it appears (whether its line
    was newline-terminated or is still the final partial segment) and removed
    only when an outcome record for it arrives.
    """

    def __init__(self, pipe) -> None:  # pipe: the raw stdout FileIO
        self._pipe = pipe
        self._lock = threading.Lock()
        self._partial = bytearray()
        self._completed: deque[bytes] = deque(maxlen=512)
        self.pending: dict[str, float] = {}  # node id -> dispatch time (ordered)
        # The single SPECULATIVE id extracted from the current unterminated
        # partial record. A read can split a node id ANYWHERE (mid-component:
        # `tests/x.py::test_fo` + `o PASSED`), so the partial's id is
        # provisional: the next absorb retires it before re-parsing the grown
        # record. Ids from COMPLETED lines are never speculative and are
        # untouched (implementation-review round 2).
        self._provisional_id: str | None = None
        self.last_activity = time.monotonic()
        self.reader_error: BaseException | None = None
        self._thread = threading.Thread(target=self._read_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read_loop(self) -> None:
        fd = self._pipe.fileno()
        while True:
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except (OSError, ValueError):
                return  # pipe closed / bad fd: child is done
            if not chunk:
                return
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except (OSError, ValueError) as exc:
                # Forwarding failed (our own stdout closed, e.g. a cancelled
                # step): record it and stop reading — silently continuing
                # would detach the monitor from the child's stream and could
                # cause a spurious quiet-arm fire (implementation-review
                # round 3 MINOR-1).
                self.reader_error = exc
                return
            self._absorb(chunk)

    def _absorb(self, chunk: bytes) -> None:
        with self._lock:
            self.last_activity = time.monotonic()
            # The previous partial's id was speculation about an unterminated
            # record; drop it before re-parsing (the record may have grown
            # anywhere — including mid-component).
            if self._provisional_id is not None:
                self.pending.pop(self._provisional_id, None)
                self._provisional_id = None
            data = self._partial + chunk
            *lines, self._partial = data.split(b"\n")
            for line in lines:
                self._ingest_line(line.decode("utf-8", errors="replace"), completed=True)
            if self._partial:
                before = set(self.pending)
                self._ingest_line(
                    self._partial.decode("utf-8", errors="replace"), completed=False
                )
                added = set(self.pending) - before
                if added:
                    # Exactly one id can be speculative: remember it for the
                    # next absorb's retirement (idempotent if unchanged).
                    self._provisional_id = next(iter(added))

    def _ingest_line(self, line: str, *, completed: bool) -> None:
        # Only the most recent occurrence decides: a result record retires the
        # id, any other appearance (dispatch prefix, partial) (re)adds it.
        ids = _NODE_ID_RE.findall(line)
        if not ids:
            if completed:
                self._completed.append(line.encode("utf-8", errors="replace"))
            return
        node = ids[-1]
        if completed and _OUTCOME_RE.search(line):
            # Retire the completed id AND any pending ids that are partial
            # prefixes of it (a node id that grew across chunk boundaries:
            # `tests/x.py::TestG` observed mid-growth, completed as
            # `tests/x.py::TestG::test_a` — the prefix must not linger as a
            # phantom pending entry; implementation-review round 1).
            self.pending.pop(node, None)
            for pending_id in [
                p for p in self.pending if node.startswith(p + "::")
            ]:
                del self.pending[pending_id]
            self._completed.append(line.encode("utf-8", errors="replace"))
        else:
            if node not in self.pending:
                self.pending[node] = time.monotonic()
            if completed:
                self._completed.append(line.encode("utf-8", errors="replace"))

    def quiet_for(self) -> float:
        with self._lock:
            return time.monotonic() - self.last_activity

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self.pending.keys())

    def tail_lines(self, count: int) -> list[str]:
        with self._lock:
            return [
                ln.decode("utf-8", errors="replace")
                for ln in list(self._completed)[-count:]
            ]


# --- descendant discovery + stack capture (plan F3/F12) ----------------------


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, ppid) from /proc/<pid>/stat, or None if gone. POSIX only."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm may contain spaces/parens: split after the last ')'.
    tail = raw[raw.rfind(")") + 1 :].split()
    if len(tail) < 2:
        return None
    return tail[0], int(tail[1])


def descendants_of(root_pid: int) -> list[tuple[int, int]]:
    """(pid, depth) for the whole live subtree, deepest first; POSIX only."""
    stats: dict[int, tuple[str, int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return [(root_pid, 0)]
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        stat = _proc_stat(pid)
        if stat is not None:
            stats[pid] = stat
    out: list[tuple[int, int]] = []
    for pid in stats:
        depth, cur = 0, pid
        while cur != root_pid:
            entry = stats.get(cur)
            if entry is None:
                break
            cur = entry[1]
            depth += 1
            if depth > 16:  # cycle guard
                break
        if cur == root_pid:
            out.append((pid, depth))
    out.sort(key=lambda item: (-item[1], item[0]))  # workers before controller
    return out


def alive(pid: int) -> bool:
    if IS_POSIX:
        stat = _proc_stat(pid)
        return stat is not None and stat[0] != "Z"
    try:  # Windows: OpenProcess existence probe
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def try_py_spy(pid: int, budget_s: float) -> bool:
    """Opportunistic external dump; denial (expected under Yama) is fine."""
    binary = shutil.which("py-spy")
    if not binary:
        return False
    try:
        proc = subprocess.run(
            [binary, "dump", "--pid", str(pid)],
            timeout=max(1.0, budget_s),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0 or not proc.stdout:
        return False
    sys.stdout.write(f"\n--- py-spy dump pid {pid} ---\n")
    sys.stdout.write(proc.stdout.decode("utf-8", errors="replace"))
    sys.stdout.flush()
    return True


def sigusr2_dump(pid: int, dump_dir: Path) -> str | None:
    """Trigger the hook's file dump and return the captured text."""
    try:
        os.kill(pid, signal.SIGUSR2)
    except (OSError, ProcessLookupError):
        return None
    path = dump_dir / f"{STACK_FILE_PREFIX}{pid}.log"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            time.sleep(0.2)  # let the writer finish the current block
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        time.sleep(0.2)
    return None


def capture_stacks(child_pid: int, budget_s: float) -> None:
    """Bounded per-PID capture chain: py-spy (nonfatal) -> SIGUSR2 file dump
    (workers before controller) -> explicit unavailable note. SIGABRT is NOT
    used here: it is the fatal last resort of the termination phase, never a
    stack provider of record (its stderr dump is invisible under xdist)."""
    started = time.monotonic()
    pids: list[tuple[int, int]]
    if IS_POSIX:
        pids = descendants_of(child_pid)
    else:
        pids = [(child_pid, 0)]
    dump_dir = _dump_dir()
    for pid, depth in pids:
        if time.monotonic() - started > budget_s:
            eprint(f"stack-dump budget exhausted before pid {pid}")
            return
        if not alive(pid):
            continue
        role = "worker" if depth > 0 else "controller"
        captured = try_py_spy(pid, min(10.0, budget_s - (time.monotonic() - started)))
        if not captured and IS_POSIX and dump_dir is not None:
            text = sigusr2_dump(pid, dump_dir)
            if text:
                sys.stdout.write(
                    f"\n--- faulthandler stacks pid {pid} ({role}) ---\n{text}\n"
                )
                sys.stdout.flush()
                try:
                    (dump_dir / f"{STACK_FILE_PREFIX}{pid}.log").unlink(missing_ok=True)
                except OSError:
                    pass
                continue
        if not captured:
            eprint(f"stack-dump unavailable for pid {pid} ({role})")


# --- termination (plan F4/F14) ------------------------------------------------


def terminate_tree(child: subprocess.Popen, deadline_s: float = 120.0) -> None:
    """Bounded group termination. POSIX: killpg TERM -> KILL with nonblocking
    reaps. Windows: taskkill /T /F (recursive). Never waits unbounded."""
    end = time.monotonic() + deadline_s

    def remaining() -> float:
        return max(0.0, end - time.monotonic())

    if IS_POSIX:
        import signal as _signal

        try:
            os.killpg(child.pid, _signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        wait = min(3.0, remaining())
        end_wait = time.monotonic() + wait
        while time.monotonic() < end_wait and child.poll() is None:
            time.sleep(0.05)
        # Unconditional bounded SIGKILL to the GROUP: the controller (direct
        # child) may have exited on SIGTERM while a TERM-resistant descendant
        # (e.g. a wedged xdist worker with SIGTERM ignored) is still alive in
        # the group — gating the kill on direct-child liveness would spare it
        # (implementation-review round 1). killpg on an all-dead group raises
        # ProcessLookupError, which is the clean outcome.
        try:
            os.killpg(child.pid, _signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(10.0, remaining()),
            )
        except (OSError, subprocess.TimeoutExpired):
            child.terminate()
    # Bounded final reap of the direct child.
    end_wait = time.monotonic() + min(5.0, remaining())
    while time.monotonic() < end_wait and child.poll() is None:
        time.sleep(0.05)
    if child.poll() is None:
        eprint("watchdog cleanup incomplete: child did not exit within budget")
    elif IS_POSIX:
        # Drain any zombie state of the direct child (bounded, best-effort).
        try:
            os.waitpid(child.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


# --- main loop ----------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    if not preflight_hook(skip=args.skip_preflight):
        eprint("stack-capture hook not ready (refusing to run the suite unmonitored)")
        return 3

    popen_kwargs: dict = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    if IS_POSIX:
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    start = time.monotonic()
    child = subprocess.Popen(args.command, **popen_kwargs)
    tracker = StreamTracker(child.stdout)
    tracker.start()

    def on_signal(signum, frame):  # noqa: ANN001 - signal handler signature
        eprint(f"watchdog received signal {signum}; cleaning up the child group")
        terminate_tree(child, deadline_s=30.0)
        raise SystemExit(128 + signum)

    if IS_POSIX:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, on_signal)

    fire_reason: str | None = None
    try:
        while True:
            if child.poll() is not None:
                break
            if tracker.reader_error is not None:
                # Our own output channel failed: the monitor is blind to the
                # child's stream, so a quiet-arm verdict would be a guess.
                # Fail bounded instead of trusting stale silence.
                fire_reason = (
                    f"reader arm: output forwarding failed "
                    f"({tracker.reader_error!r}) — refusing to monitor blind"
                )
                break
            quiet = tracker.quiet_for()
            elapsed = time.monotonic() - start
            if quiet >= args.quiet_window:
                fire_reason = f"quiet arm: no output progress for {quiet:.0f}s"
                break
            if elapsed >= args.hard_deadline:
                fire_reason = (
                    f"deadline arm: {elapsed:.0f}s elapsed >= "
                    f"{args.hard_deadline:.0f}s hard deadline"
                )
                break
            time.sleep(min(args.poll_interval, max(0.05, args.quiet_window / 8)))
    except KeyboardInterrupt:
        fire_reason = "interrupted"

    if fire_reason is None:
        code = child.returncode
        # Give the daemon reader a moment to drain, then mirror the exit code.
        time.sleep(0.2)
        return code if code is not None else 0

    elapsed = time.monotonic() - start
    pending = tracker.pending_ids()
    eprint("")
    eprint("=" * 72)
    eprint(f"WATCHDOG-FIRED: {fire_reason}")
    eprint(
        f"elapsed={elapsed:.0f}s last_output_age={tracker.quiet_for():.0f}s "
        f"child_pid={child.pid}"
    )
    if pending:
        eprint(f"pending tests ({len(pending)}):")
        for node in pending:
            eprint(f"  - {node}")
    else:
        eprint("pending tests (0): no dispatched-but-uncompleted node ids in the stream")
    tail = tracker.tail_lines(args.dump_tail)
    if tail:
        eprint(f"last {len(tail)} completed stream lines (context):")
        for line in tail:
            eprint(f"  | {line}")
    eprint("=" * 72)

    capture_stacks(child.pid, budget_s=args.dump_budget)
    terminate_tree(child, deadline_s=30.0)
    eprint(
        f"watchdog: failing the step with exit {WATCHDOG_EXIT_CODE} "
        "(named hang diagnostic above; see #1021)"
    )
    return WATCHDOG_EXIT_CODE


def main() -> int:
    args = parse_args(sys.argv[1:])
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
