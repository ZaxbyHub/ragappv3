"""Issue #702 review round-2 regression: the lease insert-failure fallback.

An exception while inserting the durable jobs row must fall back to the
legacy in-memory queue (the item's only queue entry at that point) — it must
never collapse into the dedupe-suppression outcome, which returns True with
nothing queued (silent work loss behind a "scheduled" answer).
"""

from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor


async def test_enqueue_insert_failure_falls_back_to_legacy_queue(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "b13-fallback.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert processor._ingest_lease_enabled, (  # noqa: SLF001
            "harness: the pooled processor must come up in lease mode"
        )

        real_connection = pool.connection

        class _ExplodingPool:
            """First connection() (the jobs insert) fails; later ones work."""

            def __init__(self):
                self.failed = False

            def __call__(self):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError(
                        "probe: simulated pool checkout failure"
                    )
                return real_connection()

        monkeypatch.setattr(pool, "connection", _ExplodingPool())

        enqueued = await processor.enqueue(
            "uploads/b13-fallback.txt", 1, file_id=4242
        )

        assert enqueued is True, "the fallback must still report queued"
        assert processor.queue.qsize() == 1, (
            "the item must land in the legacy in-memory queue when the "
            "durable insert raises — returning True with nothing queued is "
            "silent work loss (issue #702 review round-2 finding 1)"
        )
    finally:
        pool.close_all()
