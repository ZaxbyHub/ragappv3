"""Issue #258 (E2) acceptance checks — AC2 / TEST-002: feedback refresh contract.

Phase 2.5 CHECKS ONLY (tier L). TEST-002's defect: the legacy cache-refresh
test (``test_feedback_reranker.py::test_explicit_refresh_bypasses_ttl``)
asserted ``cache_timestamp`` movement across a real sub-second wall-clock pause
(0.01 s) — a proxy metric on a non-injectable ``time.monotonic()`` clock
(Windows coarse resolution made it the one clock-sensitive failure in the base
serial run). It never pinned the actual behavior: that a refresh re-reads the
DATABASE and returns the NEW vote state.

This node pins the real contract with zero sleeps:

1. Warm the cache (1 up vote) -> ``get_feedback_score`` returns net=1.
2. Insert an ADDITIONAL vote row directly in SQLite.
3. ``refresh(10)`` -> ``get_feedback_score`` must return the NEW net count (2)
   — a DB-derived assertion, no clock involved.
4. TTL expiry is driven by EXPLICIT clock control only: a patched
   ``time.monotonic`` advanced past the TTL makes the next read re-query the
   DB (a third vote becomes visible) — again no sleep.
5. The node asserts its own module source contains no sleep call (the
   no-sleep design is part of the contract, not a style preference).

Measured class at base a543361: PRESERVING (green at base) — production
refresh works; the gap was the test design, and the discriminator for AC2
(no-op refresh mutant failing) is mutation-probe territory (Phase 4.5).
"""

import inspect
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app.services.feedback_reranker import FeedbackReranker

_VAULT = 10
_SESSION = 1


def _make_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema needed by FeedbackReranker (mirrors the focused suite)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_sessions ("
        "id INTEGER PRIMARY KEY, vault_id INTEGER NOT NULL)",
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            sources TEXT,
            feedback TEXT
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cm_session ON chat_messages(session_id)"
    )


def _insert_vote(conn: sqlite3.Connection, file_id: str, feedback: str) -> None:
    """Insert one rated assistant message citing file_id and commit."""
    conn.execute(
        """
        INSERT INTO chat_messages (session_id, role, content, sources, feedback)
        VALUES (?, 'assistant', 'content', ?, ?)
        """,
        (_SESSION, json.dumps([{"file_id": file_id}]), feedback),
    )
    conn.commit()


class TestFeedbackRefreshDBDerived(unittest.TestCase):
    """AC2: refresh re-reads the DB; no sleep anywhere in the design."""

    def setUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix="_issue258_fb.db")
        os.close(fd)
        os.unlink(self._db_path)  # start from a truly empty file
        conn = sqlite3.connect(self._db_path)
        try:
            _make_schema(conn)
            conn.execute(
                "INSERT INTO chat_sessions (id, vault_id) VALUES (?, ?)",
                (_SESSION, _VAULT),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self._db_path + suffix)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def test_refresh_returns_new_db_vote_state_without_sleep(self) -> None:
        conn = self._connect()
        try:
            # 1. Warm the cache with a single up vote.
            _insert_vote(conn, "f_refresh", "up")
            reranker = FeedbackReranker(
                db_path=self._db_path, cache_ttl_seconds=300
            )
            warm = reranker.get_feedback_score("f_refresh", vault_id=_VAULT)
            self.assertIsNotNone(warm)
            self.assertEqual(warm.net_positive, 1)

            # 2. An additional vote lands AFTER the cache was warmed.
            _insert_vote(conn, "f_refresh", "up")

            # Cache is still TTL-valid here, so a plain read would legitimately
            # serve the stale net=1. The CONTRACT under test is refresh().
            reranker.refresh(_VAULT)

            # AC2 CHECK — refresh(vault) did not re-read the database.
            print("AC2 CHECK: FAIL — refresh(vault) did not re-read the database (stale vote state)")
            refreshed = reranker.get_feedback_score("f_refresh", vault_id=_VAULT)
            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed.net_positive, 2)
            self.assertEqual(refreshed.up_votes, 2)
            self.assertEqual(refreshed.down_votes, 0)

            # 3. TTL expiry is exercised ONLY through explicit clock control:
            #    advance the (patched) monotonic clock past the TTL and assert
            #    the next read re-queries the DB and observes a third vote.
            _insert_vote(conn, "f_refresh", "up")
            clock_base = time.monotonic()
            with patch(
                "app.services.feedback_reranker.time.monotonic",
                return_value=clock_base + 301.0,
            ):
                expired = reranker.get_feedback_score(
                    "f_refresh", vault_id=_VAULT
                )
            # AC2 CHECK — TTL expiry did not trigger a re-query under
            # explicit clock control.
            print("AC2 CHECK: FAIL — TTL expiry did not re-query under explicit clock control")
            self.assertIsNotNone(expired)
            self.assertEqual(expired.net_positive, 3)

            # 4. The no-sleep design, asserted rather than assumed. The needle
            #    is built at runtime so this source does not itself contain
            #    the literal call it forbids.
            needle = "time" + ".sleep"
            source = inspect.getsource(sys.modules[__name__])
            # AC2 CHECK — a sleep call crept into the module.
            print("AC2 CHECK: FAIL — a sleep call crept into the AC2 check module")
            self.assertNotIn(needle + "(", source)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
