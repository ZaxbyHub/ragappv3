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

`drained_unit` (issue #1068) extends the same one-unit guarantee to handlers
whose commit lives INSIDE a worker/store/helper call: the caller's whole
commit-to-audit sequence runs as one drained unit, so a cancellation landing
exactly at the inner commit await can no longer unwind the handler before the
audit coroutine exists. Its terminal settlement is shared with
`drain_commit_audit` via `_settle_drained`.
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
    existing best-effort contract). A cancellation OF the audit task itself
    (loop teardown — nothing else cancels it) writes no row and cannot be
    retried, so it is logged loudly rather than returned silently
    (swarm-pr-review F-06).
    """
    cancelled, error = await drain_await(asyncio.ensure_future(audit_coro))
    if error is not None:
        if isinstance(error, asyncio.CancelledError):
            logger.warning(
                "security_audit_record_cancelled "
                "task_cancelled_during_audit row_not_written"
            )
        else:
            logger.warning(
                "security_audit_record_failed reason=%s", type(error).__name__
            )
    if cancelled:
        raise asyncio.CancelledError


async def drained_commit(conn) -> tuple[bool, BaseException | None]:
    """Drain ONLY the commit await (issue #1043 residual 3).

    Returns ``(cancelled, error)`` and never re-raises — the caller decides:
    ``delete_vault`` re-raises ``error`` so its recovery arms still run, and
    defers the absorbed ``CancelledError`` until after its mandatory
    post-commit bookkeeping. A cancellation delivered at the commit await
    after the commit work already completed (the executor finished; the route
    task had not resumed) is absorbed until the commit is durable, then
    reported via ``cancelled``.
    """
    return await drain_await(asyncio.ensure_future(asyncio.to_thread(conn.commit)))


async def _settle_drained(
    cancelled: bool, error: BaseException | None, *, context: str
) -> None:
    """Settle one drained unit's terminal outcome (shared settlement).

    ``drain_commit_audit`` and ``drained_unit`` both reduce to
    ``drain_await`` over one unit task plus this settlement:

    - compound — the unit failed AND a caller cancellation was absorbed:
      the unit's error is logged at its own severity (expected client
      errors with a ``status_code`` < 500 at INFO without a traceback;
      everything else at ERROR with ``exc_info``) and the delivered
      cancellation WINS (``CancelledError`` re-raised after the log);
    - unit failure alone: re-raised with its original type;
    - the unit task itself cancelled (loop teardown — nothing else
      cancels it): logged as unrecoverable (no retry, row state
      unverified); with no absorbed caller cancellation the helper
      RETURNS normally after logging — unless the caller itself carries
      a pending cancellation request (the simultaneous-teardown race),
      in which case the pending cancellation is re-raised so a delivered
      cancellation never vanishes;
    - absorbed caller cancellation: ``CancelledError`` re-raised.

    Expected client errors (an HTTPException-shaped failure whose
    ``status_code`` is < 500 — duplicate-username 409s, vault rename
    409s, draft-room conflict 409s) are logged at INFO WITHOUT a stack
    trace: they are routine request outcomes, not alert-grade failures
    (swarm review PRR-003 on PR #1077 — at ERROR with ``exc_info`` they
    inflated error dashboards for every routine conflict).
    """
    if error is not None:
        if isinstance(error, asyncio.CancelledError):
            logger.warning(
                "%s_unit_cancelled unit_task_cancelled_during_%s "
                "row_state_unverified",
                context,
                context,
            )
            # Simultaneous-teardown race (swarm review PRR-006 on PR
            # #1077): the caller's cancellation was requested but never
            # DELIVERED to drain_await before the unit task's own
            # cancellation won — re-raise it so a delivered cancellation
            # never vanishes into a normal return.
            pending = asyncio.current_task().cancelling()
            if pending or cancelled:
                raise asyncio.CancelledError
        else:
            status_code = getattr(error, "status_code", None)
            if isinstance(status_code, int) and status_code < 500:
                logger.info(
                    "%s_unit_client_error reason=%s status=%s",
                    context,
                    type(error).__name__,
                    status_code,
                )
            else:
                logger.error(
                    "%s_unit_failed reason=%s",
                    context,
                    type(error).__name__,
                    exc_info=error,
                )
            if not cancelled:
                raise error
    if cancelled:
        raise asyncio.CancelledError


async def drain_commit_audit(
    conn,
    audit_factory,
    *,
    after_commit=None,
) -> None:
    """Commit + optional post-commit callback + drained audit as ONE unit
    (issue #1043 residual 3).

    Unit: ``to_thread(conn.commit)`` -> ``after_commit()`` (if given) ->
    ``drain_audit(audit_factory())``. The audit coroutine is created lazily
    by ``audit_factory`` so it only exists once the commit has completed
    (record-after-real-mutation ordering preserved). ``after_commit`` exists
    so cache invalidations that today sit BETWEEN the commit and the audit
    (e.g. ``invalidate_active_user_cache`` in users.py) stay inside the
    drained unit instead of widening the stale-cache window.

    Outcome contract:

    - commit failure: the unit fails, the error is re-raised;
      ``after_commit`` NOT run; the audit NOT created (the mutation was not
      applied — the same ordering as today's non-cancel error path).
    - ``after_commit`` failure: re-raised; audit not created (today's
      ordering — whatever sits between commit and audit could already skip
      it on error — preserved).
    - cancellation of the caller at any point (including exactly at the
      commit await after the commit work completed): absorbed until the
      whole unit completes — commit durable, callback run, audit row written
      — then ``CancelledError`` re-raised.
    - audit failure: logged by ``drain_audit``, not re-raised (the
      best-effort audit contract).
    - compound — unit failure AND an absorbed caller cancellation: the
      unit's error is logged loudly (``exc_info``) and the delivered
      cancellation WINS — ``CancelledError`` re-raised after the log (a
      delivered cancellation never vanishes; the failure is not silently
      dropped either).
    - the unit task itself cancelled (loop teardown — nothing else cancels
      it): logged as unrecoverable (no retry, row state unverified — the
      audit may or may not have written depending on where the cancellation
      landed); if no caller cancellation was absorbed this helper RETURNS
      normally after logging; if the caller itself carries a pending
      cancellation request (the simultaneous-teardown race, PRR-006) the
      pending cancellation is re-raised so a delivered cancellation never
      vanishes; an absorbed caller cancellation still re-raises
      ``CancelledError`` after the log.
    """

    async def _unit() -> None:
        await asyncio.to_thread(conn.commit)
        if after_commit is not None:
            after_commit()
        await drain_audit(audit_factory())

    cancelled, error = await drain_await(asyncio.ensure_future(_unit()))
    await _settle_drained(cancelled, error, context="commit_audit")


async def drained_unit(unit: Coroutine) -> None:
    """Commit-bearing unit + mandatory audit tail as ONE drained unit, for
    handlers whose commit lives INSIDE a worker/store/helper call (the
    handler never owns the connection at commit time, so
    ``drain_commit_audit``'s ``conn`` shape cannot apply).

    ``unit`` is the caller's existing commit-to-audit sequence as one
    async closure. Outcome contract (``_settle_drained``):

    - a caller cancellation delivered while the unit runs (including
      exactly at the inner commit await, after the commit work
      completed) is absorbed until the unit settles, then
      ``CancelledError`` re-raised;
    - a cancellation BEFORE the unit starts needs no audit: no mutation
      ran, so the handler unwinds without ever creating the unit;
    - an ordinary unit failure AFTER the commit (a post-commit step such
      as token creation, cookie setting, or cache invalidation)
      propagates with its original type and SKIPS the audit — exact
      ``drain_commit_audit`` parity for whatever sits between the commit
      and the audit tail;
    - an audit-write failure inside the unit is logged, never raised
      (``drain_audit`` best-effort);
    - an inner-unit cancellation (loop teardown): loud warning + return —
      except the simultaneous-teardown race (the caller carries a pending
      cancellation request), where the caller's cancellation is re-raised
      (PRR-006).
    """
    cancelled, error = await drain_await(asyncio.ensure_future(unit))
    await _settle_drained(cancelled, error, context="drained_unit")
