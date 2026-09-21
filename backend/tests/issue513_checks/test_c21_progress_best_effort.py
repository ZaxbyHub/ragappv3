"""Issue #513 AC21 (INGEST-012): terminal progress cleanup is best-effort.

``set_phase`` and ``clear_progress`` document their DB writes as best-effort
("a progress-update failure must never abort indexing"). A pool whose
checkout raises — the expected pool-exhaustion/closed-pool outcome, a
``RuntimeError`` — must be absorbed (logged) by both helpers, exactly as
``sqlite3.Error`` already is. Ingestion must therefore return success without
requeue when the pool is only exhausted during progress cleanup.

Since #645 the helpers are ``async def`` and check out via
``pool.get_connection_async()``; the stub raises from that surface and each
helper call is driven to completion on a real event loop (``asyncio.run``),
so the best-effort absorption is exercised end-to-end.

DISCRIMINATING: at the pre-fix commit the helpers catch only ``sqlite3.Error``
(document_progress.py), so the RuntimeError propagates and this script prints
``C21 CHECK: FAIL: ...`` and exits 1.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c21_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP


class ExhaustedPool:
    """Pool stub whose checkout always raises, like a drained/closed pool."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def connection(self):
        raise self._exc

    async def get_connection_async(self, max_wait_attempts: int = 3):
        raise self._exc


def _absorbed(helper, pool, label: str) -> str | None:
    """Run one helper call to completion; return a FAIL reason if the error
    propagates."""
    try:
        asyncio.run(helper(pool, 1))
    except Exception as exc:  # noqa: BLE001 - verdict, not crash
        return (
            f"{label} propagated {type(exc).__name__} from pool exhaustion "
            f"instead of absorbing it (best-effort contract broken): {exc}"
        )
    return None


def main() -> int:
    import logging

    # The best-effort absorption is expected to log warnings; keep the script
    # output to exactly one verdict line.
    logging.disable(logging.CRITICAL)

    from app.services.document_progress import clear_progress, set_phase

    # sqlite3.Error absorption is already the documented behavior (preserving).
    reason = _absorbed(
        lambda p, fid: set_phase(
            p, fid, phase="parsing", message="m", percent=1.0, mark_processing_started=True
        ),
        ExhaustedPool(sqlite3.OperationalError("pool exhausted")),
        "set_phase(sqlite3.Error)",
    )
    if reason:
        print(f"C21 CHECK: FAIL: {reason}")
        return 1
    reason = _absorbed(
        clear_progress, ExhaustedPool(sqlite3.OperationalError("pool exhausted")),
        "clear_progress(sqlite3.Error)",
    )
    if reason:
        print(f"C21 CHECK: FAIL: {reason}")
        return 1

    # RuntimeError (pool exhaustion / closed pool from database.py) must be
    # equally best-effort: expected operational noise, not a crash.
    reason = _absorbed(
        lambda p, fid: set_phase(
            p, fid, phase="parsing", message="m", percent=1.0, mark_processing_started=True
        ),
        ExhaustedPool(RuntimeError("Connection pool exhausted")),
        "set_phase(RuntimeError)",
    )
    if reason:
        print(f"C21 CHECK: FAIL: {reason}")
        return 1
    reason = _absorbed(
        clear_progress, ExhaustedPool(RuntimeError("Connection pool exhausted")),
        "clear_progress(RuntimeError)",
    )
    if reason:
        print(f"C21 CHECK: FAIL: {reason}")
        return 1

    print("C21 CHECK: PASS")
    return 0


def test_c21_progress_best_effort():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
