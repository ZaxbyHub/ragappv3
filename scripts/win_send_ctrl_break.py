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
import sys

kernel32 = ctypes.windll.kernel32

CTRL_BREAK_EVENT = 1
ATTACH_PARENT_PROCESS = ctypes.c_uint(-1).value  # noqa: F841 — documented constant


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: win_send_ctrl_break.py <group-leader-pid>", file=sys.stderr)
        return 2
    pid = int(sys.argv[1])

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        err = ctypes.get_last_error()
        print(f"win_send_ctrl_break: AttachConsole({pid}) failed err={err}")
        return 3
    try:
        if not kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid):
            err = ctypes.get_last_error()
            print(f"win_send_ctrl_break: GenerateConsoleCtrlEvent failed err={err}")
            return 4
    finally:
        # Brief linger so the event is observed on the shared console before
        # this process detaches; the detach never affects the target.
        import time

        time.sleep(0.4)
        kernel32.FreeConsole()
    print(f"win_send_ctrl_break: CTRL_BREAK delivered to group {pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
