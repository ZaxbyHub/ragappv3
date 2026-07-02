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

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def get_toggle(self, feature: str, default: bool = False) -> bool:
        now = time.time()
        with self._lock:
            entry = self._cache.get(feature)
            if entry and now - entry.timestamp < self.CACHE_TTL:
                return entry.enabled
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
            self._cache[feature] = ToggleCacheEntry(timestamp=now, enabled=value)
        return value

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def set_toggle(self, feature: str, enabled: bool) -> None:
        conn = self.pool.get_connection()
        try:
            self.set_toggle_on_connection(conn, feature, enabled)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.release_connection(conn)
        self.update_cache(feature, enabled)

    def set_toggle_on_connection(
        self, conn: sqlite3.Connection, feature: str, enabled: bool
    ) -> None:
        """Write a toggle using the caller's transaction."""
        conn.execute(self._UPSERT_SQL, (feature, int(enabled)))

    def update_cache(self, feature: str, enabled: bool) -> None:
        """Update the in-memory cache after a durable commit."""
        with self._lock:
            self._cache[feature] = ToggleCacheEntry(
                timestamp=time.time(), enabled=enabled
            )

    def clear_cache(self, feature: Optional[str] = None) -> None:
        with self._lock:
            if feature:
                self._cache.pop(feature, None)
            else:
                self._cache.clear()
