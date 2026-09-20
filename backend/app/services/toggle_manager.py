"""Toggle manager with caching and persistence."""

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.models.database import SQLiteConnectionPool
from app.utils.retry import with_retry


@dataclass
class ToggleCacheEntry:
    timestamp: float
    enabled: bool


class ToggleManager:
    """Reads and writes feature toggles backed by SQLite."""

    CACHE_TTL = 30.0  # seconds
    _UPSERT_SQL = (
        "INSERT INTO admin_toggles(feature, enabled) VALUES(?, ?) "
        "ON CONFLICT(feature) DO UPDATE SET "
        "enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP"
    )

    def __init__(self, pool: SQLiteConnectionPool) -> None:
        self.pool = pool
        self._cache: dict[str, ToggleCacheEntry] = {}
        self._lock = threading.Lock()
        # Bumped on every invalidation (issue #596): a cache-miss read
        # snapshots the generation before its pooled DB read and only writes
        # the result back if the generation still matches — a toggle that
        # commits mid-read invalidates the cache and the stale read is
        # discarded instead of re-poisoning it for the full TTL.
        self._generation = 0

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def get_toggle(self, feature: str, default: bool = False) -> bool:
        now = time.time()
        with self._lock:
            entry = self._cache.get(feature)
            if entry and now - entry.timestamp < self.CACHE_TTL:
                return entry.enabled
            generation = self._generation
        conn = self.pool.get_connection()
        try:
            row = conn.execute(
                "SELECT enabled FROM admin_toggles WHERE feature = ?",
                (feature,)
            ).fetchone()
            value = default if row is None else bool(row["enabled"])
        finally:
            self.pool.release_connection(conn)
        with self._lock:
            if generation == self._generation:
                self._cache[feature] = ToggleCacheEntry(timestamp=now, enabled=value)
        return value

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def set_toggle(self, feature: str, enabled: bool) -> None:
        conn = self.pool.get_connection()
        try:
            self.set_toggle_on_connection(conn, feature, enabled)
            self.commit_and_publish(conn, feature, enabled)
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.release_connection(conn)

    def set_toggle_on_connection(
        self, conn: sqlite3.Connection, feature: str, enabled: bool
    ) -> None:
        """Write a toggle using the caller's transaction."""
        conn.execute(self._UPSERT_SQL, (feature, int(enabled)))

    def commit_and_publish(
        self, conn: sqlite3.Connection, feature: str, enabled: bool
    ) -> None:
        """Commit the caller's durable toggle transaction and publish the new
        value to the cache as one atomic step (issue #603).

        Holding ``_lock`` across both means no other toggle writer can commit
        between this commit and its cache publish, so the cache can never
        hold a value older than the latest durable commit. The publish is
        inlined rather than delegated to ``update_cache`` because that lock
        is not reentrant. Writers acquire SQLite's write lock (BEGIN
        IMMEDIATE or the upsert's implicit transaction) before taking
        ``_lock``, so the two locks can never cycle.
        """
        with self._lock:
            try:
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            self._cache[feature] = ToggleCacheEntry(
                timestamp=time.time(), enabled=enabled
            )
            self._generation += 1

    def update_cache(self, feature: str, enabled: bool) -> None:
        """Update the in-memory cache after a durable commit.

        Also bumps the cache generation so in-flight stale reads (started
        before this invalidation) are discarded instead of written back.

        Writers should prefer ``commit_and_publish``, which commits the
        durable transaction and publishes to the cache as one atomic step;
        calling this separately after ``conn.commit()`` leaves a
        writer-vs-writer ordering window (issue #603).
        """
        with self._lock:
            self._cache[feature] = ToggleCacheEntry(
                timestamp=time.time(), enabled=enabled
            )
            self._generation += 1

    def clear_cache(self, feature: Optional[str] = None) -> None:
        with self._lock:
            self._generation += 1
            if feature:
                self._cache.pop(feature, None)
            else:
                self._cache.clear()
