"""Arm's-length acceptance check for issue #786, defect class 2 (AC4):
``_compensate`` must run its ``cancel_pending_jobs`` call OFF the event-loop
thread.

``app.services.draft_promotion._compensate`` (:583) calls
``background_processor.cancel_pending_jobs(file_id=...)`` synchronously from
an async frame. In lease mode (``settings.ingestion_job_lease_enabled=True``
default, config.py:321) that method's lease branch
(background_tasks.py:3597-3622) does a blocking
``processor.pool.connection()`` checkout — on the event-loop thread. This
check pins the thread identity of that checkout against the thread running
the event loop, against a REAL ``BackgroundProcessor`` over a REAL
``SQLiteConnectionPool`` on a tmp DB (the test_559_lease_integration.py
pattern), so the base failure renders ``assert <tid> != <tid>`` (identical
integers both sides).
"""

import asyncio
import os
import sys
import threading
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.services.background_tasks import BackgroundProcessor  # noqa: E402
from app.services.draft_promotion import _compensate  # noqa: E402


def test_compensate_cancels_jobs_off_the_event_loop(tmp_path):
    """cancel_pending_jobs' pooled checkout must not happen on the loop thread."""
    db_path = str(tmp_path / "n02_compensate.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)

    checkout_threads: list[int] = []
    real_connection = pool.connection

    @contextmanager
    def recording_connection():
        checkout_threads.append(threading.get_ident())
        with real_connection() as conn:
            yield conn

    pool.connection = recording_connection

    async def _drive() -> int:
        bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert bp._ingest_lease_enabled is True, (
            "lease mode must be on by default for a real pool "
            "(settings.ingestion_job_lease_enabled=True)"
        )
        loop_thread = threading.get_ident()
        await _compensate(
            None,
            background_processor=bp,
            promotion_id=None,
            file_id=None,
            dest_path=None,
            enqueued_file_id=424242,
        )
        return loop_thread

    try:
        loop_thread = asyncio.run(_drive())
    finally:
        pool.close_all()

    assert checkout_threads, (
        "cancel_pending_jobs must check out a pooled connection in lease mode"
    )
    checkout_thread = checkout_threads[0]
    assert checkout_thread != loop_thread, (
        f"cancel_pending_jobs checked out the pool on the event-loop thread ({loop_thread})"
    )
