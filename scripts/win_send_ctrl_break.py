"""Sacrificial CTRL_BREAK sender for the Windows graceful stop (#799/#854).

Run as its own short-lived process by stop-services.ps1 / start-services.ps1:
    python scripts/win_send_ctrl_break.py <group-leader-pid>

This process detaches from its own console, attaches to the target's console,
and sends CTRL_BREAK_EVENT to the target's NONZERO process group (the backend
is created as a group leader by scripts/win_grouped_backend.py). CPython maps
CTRL_BREAK_EVENT to SIGBREAK; uvicorn handles SIGBREAK and runs the graceful
lifespan shutdown. Because THIS process is the one that detaches/attaches, the
calling script's console and stdout handles are never touched — there is
nothing to restore. Exit code 0 = signal delivered; 3 = attach failed; 4 =
event failed; prints a diagnostic line either way.
"""

import ctypes
import os
import sys

kernel32 = None

CTRL_BREAK_EVENT = 1
ATTACH_PARENT_PROCESS = ctypes.c_uint(-1).value  # noqa: F841 — documented constant

# The exit statuses this script defines. The launcher reads them to tell
# "the signal was not delivered" from "the signal was delivered": only
# these three mean a delivery failure.
EXIT_USAGE = 2
EXIT_ATTACH_FAILED = 3
EXIT_EVENT_FAILED = 4
EXIT_DELIVERED = 0


def _kernel32():
    """Bind kernel32 on first use.

    The binding is deferred (instead of at import) so the module can be
    imported on a non-Windows host -- the exit-status semantics below are
    then testable anywhere -- while the Windows path binds exactly as
    before.
    """
    global kernel32
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return kernel32


def _say(message: str, stream=None) -> None:
    """Print a diagnostic, tolerating a broken standard handle.

    The launcher starts this script with ``-WindowStyle Hidden``, which can
    leave the child without a usable standard handle: the print would then
    raise, and an unhandled exception would replace the script's exit
    status with 1 (or with 120 when the interpreter's exit flush fails as
    well) -- a status that says nothing about the signal. A diagnostic is
    never worth the status: the outcome is carried by the exit code.
    """
    target = sys.stdout if stream is None else stream
    try:
        print(message, file=target)
    except Exception:
        pass


def _exit(code: int) -> None:
    """Leave with EXACTLY ``code`` as the process status.

    The interpreter's own exit flushes stdout/stderr. When the inherited
    standard handles are not usable -- the launcher starts this script with
    ``-WindowStyle Hidden``, which leaves the child without a console it can
    write to -- that flush raises, and CPython then exits with 120 (its
    "could not flush stdout at exit" status). 120 says nothing about the
    signal: the launcher would read a DELIVERED signal as a failure and
    shorten the graceful window, force-killing a shutdown that is still
    inside the enforced envelope. Flush explicitly (a broken handle must not
    change the status) and leave through os._exit, which skips the
    interpreter's exit-time flush entirely.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)


def main() -> int:
    if len(sys.argv) != 2:
        _say("usage: win_send_ctrl_break.py <group-leader-pid>", sys.stderr)
        return EXIT_USAGE
    pid = int(sys.argv[1])

    kernel32 = _kernel32()
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        err = ctypes.get_last_error()  # use_last_error=True binding
        _say(f"win_send_ctrl_break: AttachConsole({pid}) failed err={err}")
        return EXIT_ATTACH_FAILED
    try:
        if not kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid):
            err = ctypes.get_last_error()
            _say(f"win_send_ctrl_break: GenerateConsoleCtrlEvent failed err={err}")
            return EXIT_EVENT_FAILED
    finally:
        # Brief linger so the event is observed on the shared console before
        # this process detaches; the detach never affects the target.
        import time

        time.sleep(0.4)
        kernel32.FreeConsole()
    _say(f"win_send_ctrl_break: CTRL_BREAK delivered to group {pid}")
    return EXIT_DELIVERED


if __name__ == "__main__":
    _exit(main())
