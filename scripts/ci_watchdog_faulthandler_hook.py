"""sitecustomize hook for the Backend CI watchdog's file-based stack capture.

Loaded via PYTHONPATH by every Python process in the Backend Test step (the
CI workflow copies this file to ``<runner.temp>/watchdog-faulthandler-hook/
sitecustomize.py``; tests copy it the same way — ONE source, never
re-authored, so the CI wiring and the proof harness cannot drift).

On POSIX it opens a per-PID dump file under WATCHDOG_DUMP_DIR, keeps the
binary-unbuffered file object alive for the process lifetime (faulthandler
requires an open file object or descriptor — a path string raises TypeError),
and registers ``faulthandler.register(SIGUSR2, file=...)``. The watchdog
sends SIGUSR2 from OUTSIDE; faulthandler's C-level handler dumps all thread
stacks WITHOUT the GIL and to a FILE, so wedged xdist workers' frames survive
pytest's capture (a stderr dump is swallowed by it — issue-tracer trace
1021-1038, plan-critic round 3 probe).

On success it writes a readiness marker so the watchdog's fail-closed
preflight can prove the hook is live before the suite starts (round-4 F18).
Non-POSIX hosts: deliberate no-op (no SIGUSR2; the preflight discloses it).
"""

import faulthandler
import os
import signal

_MARKER = "watchdog-faulthandler-hook-ready"

if os.name == "posix":
    try:
        _dump_dir = os.environ.get("WATCHDOG_DUMP_DIR")
        if _dump_dir:
            _path = os.path.join(_dump_dir, f"wd-stacks-{os.getpid()}.log")
            # Binary unbuffered: faulthandler writes raw bytes from its C
            # handler; buffering would risk losing the tail on abort.
            _wd_dump_file = open(_path, "wb", buffering=0)  # noqa: SIM115
            faulthandler.register(
                signal.SIGUSR2, file=_wd_dump_file, all_threads=True
            )
            _ready_path = os.path.join(
                _dump_dir, f"wd-hook-ready-{os.getpid()}.log"
            )
            with open(_ready_path, "w", encoding="utf-8") as _ready:
                _ready.write(_MARKER + "\n")
    except Exception:  # noqa: BLE001 - a broken hook must not brick the step
        # The watchdog's preflight fails the step closed if the marker never
        # appears; stay silent here so the failure is attributed there.
        pass
