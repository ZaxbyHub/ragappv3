"""Grouped backend launcher for the Windows native workflow (#799/#854).

start-services.ps1 routes the backend through this wrapper so the uvicorn
process is created with CREATE_NEW_PROCESS_GROUP — a process-GROUP leader
(pgid == pid) sharing this wrapper's console. stop-services.ps1 then
delivers a TARGETED CTRL_BREAK to that nonzero group id (CPython maps it to
SIGBREAK; uvicorn handles SIGBREAK) for a graceful native stop, after
verifying ownership against the pid/FILETIME marker this wrapper writes.

Usage: python scripts/win_grouped_backend.py <backend-python> <uvicorn args...>
Exit status: the child's.
"""

import os
import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: win_grouped_backend.py <backend-python> <uvicorn args...>",
            file=sys.stderr,
        )
        return 2
    child = subprocess.Popen(
        [sys.argv[1]] + sys.argv[2:],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    # Approximate .NET FILETIME ticks (100ns since 1601-01-01) for the
    # child's start; the stop script compares against Get-Process StartTime
    # within a tolerance, so wall-clock precision is sufficient for PID-reuse
    # detection (a reused PID differs by minutes, not microseconds).
    start_ticks = int(time.time() * 10_000_000 + 116444736000000000)
    marker_dir = os.environ.get("RAGAPP_MARKER_DIR", ".")
    try:
        os.makedirs(os.path.join(marker_dir, ".cache"), exist_ok=True)
        with open(
            os.path.join(marker_dir, ".cache", "backend.pid"), "w"
        ) as fh:
            fh.write("%d %d\n" % (child.pid, start_ticks))
    except OSError:
        pass
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
