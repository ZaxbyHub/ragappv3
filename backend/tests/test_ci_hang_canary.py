"""Committed canaries for the Backend CI hang class (issue #1021).

The production wedge (four exactly-60-minute Backend job stalls on
2026-10-21..24, including a master run) held worker main threads in ways
where the POSIX-default signal timeout behaves differently per class:

* a GIL-releasing C call (``hashlib.pbkdf2_hmac`` — a monitor thread ticks
  while it runs) keeps the main thread inside C until the call returns, so
  the signal handler fires only LATE on bounded calls and NEVER on
  unbounded ones;
* a starved-pool join IS interrupted by the signal method on POSIX (the CI
  contrast leg shows the per-test ``Failed: Timeout`` failure) and by the
  thread method;
* a native call that HOLDS the GIL starves both the signal handler and the
  timer thread — the disclosed residual class neither method covers.

The Backend job's canary step runs these wedges under
``--timeout-method=thread --timeout=30`` and expects the fast abort: the
``Timeout`` banner, a stack dump naming the wedged frame, and exit 1 via
pytest-timeout's ``os._exit``. The Test step's own use of the flag is
pinned separately by ``backend/tests/test_1021_timeout_wiring.py`` (the
canary passes its own flags and cannot detect a revert of the production
command lines).

At the pre-fix base this file is absent (frozen check C1's NEW-SURFACE
signature); at head the check proves the abort within bounded wall clock.

The module-level skip keeps the canaries out of the normal suite: they run
only when RUN_CI_HANG_CANARY=1 is set explicitly. Because the wedges do
not assert anything themselves, a stray export of that variable in an
environment WITHOUT a per-test timeout configured would otherwise hang the
suite forever on the pool-join wedge — the module watchdog below bounds
that: if no timeout aborts the run within 120s of the wedges starting, the
process is killed loudly with exit code 3.
"""

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

if os.environ.get("RUN_CI_HANG_CANARY") != "1":
    pytest.skip(
        "hang-class canary: runs only under its dedicated CI step "
        "(RUN_CI_HANG_CANARY=1); see issue #1021",
        allow_module_level=True,
    )


def _stray_export_watchdog() -> None:
    time.sleep(120)
    print(
        "RUN_CI_HANG_CANARY=1 is set but no per-test timeout fired within "
        "120s — killing this runaway canary/wedge run (exit 3). If you did "
        "not mean to run the hang canaries, unset RUN_CI_HANG_CANARY.",
        flush=True,
    )
    os._exit(3)


threading.Thread(target=_stray_export_watchdog, daemon=True).start()

# ~250 s at the measured 2.39M iterations/s (2026-10-27 box) — an ~8.4x
# margin over the 30 s per-test timeout the canary step configures, still a
# ~2.1x margin on a runner four times faster than the measuring host.
# Slower runners are irrelevant: the timer thread caps the leg at 30 s
# regardless of how long the wedge would eventually take.
_ITERATIONS = 600_000_000


def test_wedge_c_block_aborted_by_configured_timeout():
    # One C call: GIL released (timer thread can fire), main thread never
    # returns to the interpreter loop (signal handler cannot fire).
    hashlib.pbkdf2_hmac(
        "sha256", b"canary-payload", b"canary-salt", _ITERATIONS
    )


def test_wedge_starved_pool_join_aborted_by_configured_timeout():
    # The issue-named signature. Workers block on the Event (no self-release
    # timer) while the main thread waits on future.result() with no timeout.
    # The finally clause exists for the signal-method contrast leg: CPython's
    # executor exit hook joins blocked workers at interpreter shutdown, so
    # the worker must be released before pytest can report the timeout
    # failure. The thread-method legs skip it by design (os._exit(1) kills
    # the process first).
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=2)

    def _blocked_worker():
        release.wait()

    try:
        future = executor.submit(_blocked_worker)
        future.result()
    finally:
        release.set()
        executor.shutdown(wait=True)
