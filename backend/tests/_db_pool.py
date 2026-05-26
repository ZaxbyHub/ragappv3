"""Shared test connection pool.

A minimal thread-safe SQLite connection pool used by route tests to back the
`get_db` dependency override. This was previously copy-pasted into every test
module; it now lives here so there is a single definition (DD-C014).

It is intentionally importable by bare module name (`from _db_pool import
SimpleConnectionPool`): the test files prepend the backend directory to
sys.path, and there are two `conftest.py` modules in the tree (backend root and
backend/tests), so importing from `conftest` would be ambiguous. A uniquely
named helper module avoids that collision.
"""

import sqlite3
import threading
from queue import Empty, Queue


class SimpleConnectionPool:
    """Thread-safe SQLite pool with the get/release/close_all interface the
    route tests expect. Connections enable foreign keys and use Row factory."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._pool: Queue = Queue(maxsize=5)
        self._lock = threading.Lock()
        self._closed = False

    def get_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        if self._closed:
            conn.close()
            return
        try:
            self._pool.put_nowait(conn)
        except Exception:
            conn.close()

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except Empty:
                break


__all__ = ["SimpleConnectionPool"]
