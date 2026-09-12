"""Maintenance mode flag service."""

from __future__ import annotations

import sqlite3
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

    def __init__(self, pool: SQLiteConnectionPool) -> None:
        self.pool = pool
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
