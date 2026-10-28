"""Exception-safe task draining for mandatory bookkeeping (issue #1036).

`drain_await` is the drained-completion pattern PR #1035 shipped inside
delete_user (originally `_drain`), extracted to a shared helper so the
post-commit bookkeeping sites — security-audit recording and purge
restoration — can complete through repeated task cancellations instead of
being skipped whenever a client disconnect lands on their await.

`drain_audit` applies that pattern to a `safe_record_security_event(...)`
coroutine created at the CALL SITE: the route module's (possibly decorated)
`safe_record_security_event` reference is resolved when the coroutine is
created, so the audit still records through a cancellation delivered to the
route task, and the cancellation is re-raised only after the row is written.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)


async def drain_await(task: asyncio.Future) -> tuple[bool, BaseException | None]:
    """Await ``task`` through repeated cancellations of the caller.

    Returns ``(cancelled, error)``:

    - ``cancelled`` — whether a cancellation of the CALLING task was absorbed
      while waiting (the caller should re-raise ``asyncio.CancelledError``
      after its mandatory work completes, never swallow it);
    - ``error`` — the drained task's own failure (``Exception`` subclass or
      the task having been cancelled itself), or ``None``.

    A task cancelled before the caller could observe its result terminates
    the loop (``task.cancelled()``) instead of retrying forever.
    """
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            return cancelled, None
        except asyncio.CancelledError as exc:
            if task.cancelled():
                # The drained task itself was cancelled (e.g. loop
                # shutdown) — return rather than loop forever.
                return cancelled, exc
            cancelled = True
            continue
        except Exception as exc:
            return cancelled, exc


async def drain_audit(audit_coro: Coroutine) -> None:
    """Record a security-audit event through repeated cancellations.

    ``audit_coro`` is the `safe_record_security_event(...)` coroutine object,
    created by the route module at the call site. A cancellation delivered to
    the route task while this awaits is absorbed until the audit coroutine
    finishes, then re-raised so the request still unwinds as cancelled; a
    failure of the audit itself is logged, never re-raised (the helper's
    existing best-effort contract).
    """
    cancelled, error = await drain_await(asyncio.ensure_future(audit_coro))
    if error is not None and not isinstance(error, asyncio.CancelledError):
        logger.warning(
            "security_audit_record_failed reason=%s", type(error).__name__
        )
    if cancelled:
        raise asyncio.CancelledError
