"""Recovery-critical work registry for the bounded shutdown tail exit.

Issue #799 / #854 closeout architecture (plan-critic rounds 6-10): the
lifespan shutdown's bounded tail exit may call ``os._exit(0)`` only when it
can prove that every recovery-critical workflow has completed. This module
provides the deny-by-default registry that makes that predicate total by
construction: protected recovery sequences (user-delete durability, vault
rollback/restore, draft-promotion compensation, memory-transaction rollback,
document-upload compensation, turn finalization, upload commits) run inside
the ``critical_shutdown_work`` context manager while active; the gate
requires the registry to be EMPTY. Anything unregistered is by definition
not recovery-critical, and the Phase 4.2 sweep (absorbed-cancellation /
shield / post-commit-durability patterns) owns the registration map.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

_lock = threading.Lock()
_active: dict[str, int] = {}
_total_entered = 0


def active_critical_work() -> dict[str, int]:
    """Snapshot of the currently-active critical workflows (name -> count)."""
    with _lock:
        return dict(_active)


def critical_work_active() -> bool:
    """True while any registered recovery-critical workflow is live."""
    with _lock:
        return bool(_active)


@asynccontextmanager
async def critical_shutdown_work(name: str) -> AsyncIterator[None]:
    """Register a recovery-critical sequence for the duration of the block.

    The count is exception-safe (decremented on every exit path) and nests
    (the same name may be entered reentrantly). The bounded tail exit reads
    ``critical_work_active()`` AFTER its final logging flush with no await
    between the check and the exit, so a workflow that has entered cannot be
    cut, and one that has exited is provably complete.
    """
    global _total_entered
    with _lock:
        _active[name] = _active.get(name, 0) + 1
        _total_entered += 1
    try:
        yield
    finally:
        with _lock:
            count = _active.get(name, 0) - 1
            if count > 0:
                _active[name] = count
            else:
                _active.pop(name, None)
