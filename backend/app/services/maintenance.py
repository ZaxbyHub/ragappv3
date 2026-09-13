"""Maintenance mode flag service."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.models.database import SQLiteConnectionPool
from app.utils.retry import with_retry


class MaintenanceError(Exception):
    pass


@dataclass
class MaintenanceFlag:
    enabled: bool
    reason: str
    version: int
    updated_at: Optional[str]


class MaintenanceService:
    FLAG_NAME = "maintenance"

    # Bounded-staleness cache for the request path (issue #549 C02): 5 s
    # bounds how long a foreign worker's toggle can stay unseen, while
    # set_flag's invalidation keeps same-process toggles immediate
    # (issue #549 AC2 "within a short TTL").
    FLAG_CACHE_TTL_SECONDS = 5.0

    def __init__(
        self,
        pool: SQLiteConnectionPool,
        flag_cache_ttl_seconds: float = FLAG_CACHE_TTL_SECONDS,
    ) -> None:
        self.pool = pool
        self.flag_cache_ttl_seconds = float(flag_cache_ttl_seconds)
        self._flag_cache: Optional[MaintenanceFlag] = None
        self._flag_cache_at: float = 0.0
        self._flag_cache_lock = threading.Lock()
        self._ensure_flag_row()

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def _ensure_flag_row(self) -> None:
        conn = self.pool.get_connection()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO system_flags(name, value, version, reason)
                VALUES (?, 0, 0, '')
                """,
                (self.FLAG_NAME,)
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def get_flag(self) -> MaintenanceFlag:
        conn = self.pool.get_connection()
        try:
            row = conn.execute(
                "SELECT value, reason, version, updated_at FROM system_flags WHERE name = ?",
                (self.FLAG_NAME,),
            ).fetchone()
            if row is None:
                raise MaintenanceError("Maintenance flag missing")
            return MaintenanceFlag(
                enabled=bool(row[0]),
                reason=row[1] or "",
                version=row[2] or 0,
                updated_at=row[3],
            )
        finally:
            self.pool.release_connection(conn)

    def get_flag_cached(self) -> MaintenanceFlag:
        """Return the flag, serving reads from a short in-process TTL cache.

        The pooled read still happens on a cache miss/expiry — callers on the
        event loop must invoke this via ``get_flag_async`` (a worker thread),
        never directly (issue #549 C02). Concurrent misses may race; the lock
        makes population idempotent and the loser's read is simply discarded.
        """
        now = time.monotonic()
        with self._flag_cache_lock:
            if (
                self._flag_cache is not None
                and now - self._flag_cache_at < self.flag_cache_ttl_seconds
            ):
                return self._flag_cache
        flag = self.get_flag()
        with self._flag_cache_lock:
            self._flag_cache = flag
            self._flag_cache_at = time.monotonic()
        return flag

    async def get_flag_async(self) -> MaintenanceFlag:
        """Off-the-event-loop flag read for request-path callers."""
        return await asyncio.to_thread(self.get_flag_cached)

    def _invalidate_flag_cache(self) -> None:
        with self._flag_cache_lock:
            self._flag_cache = None
            self._flag_cache_at = 0.0

    def set_flag(self, enabled: bool, reason: str = "") -> None:
        attempts = 0
        while True:
            flag = self.get_flag()
            conn = self.pool.get_connection()
            try:
                cursor = conn.execute(
                    """
                    UPDATE system_flags
                    SET value = ?, reason = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE name = ? AND version = ?
                    """,
                    (int(enabled), reason, self.FLAG_NAME, flag.version),
                )
                if cursor.rowcount:
                    conn.commit()
                    # Same-process toggles must be visible to the very next
                    # request (issue #549 AC2): drop the cached value only
                    # after the committed UPDATE, so a failed update leaves
                    # the previously cached state intact.
                    self._invalidate_flag_cache()
                    return
                # Optimistic-lock miss: the UPDATE matched no rows, so end the
                # implicit transaction it opened before this connection
                # returns to the pool instead of relying on the pool's
                # release-time rollback (issue #548).
                conn.rollback()
            finally:
                self.pool.release_connection(conn)
            attempts += 1
            if attempts > 3:
                raise MaintenanceError("Failed to update maintenance flag")
