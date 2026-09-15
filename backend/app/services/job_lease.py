"""DB-claimed job lease primitive (issue #559).

One claim/heartbeat/janitor/fencing code path for the background-job workers.
A job is either owned by exactly one live lease (the claim statement below
guarantees exclusivity; ``heartbeat`` renews it; fenced writes bound a
worker's authority to the lease it still holds) or settled (terminal, or back
to ``pending`` via ``reclaim_expired``) — never running while owned by
nothing, and never writable by a stale owner.

The claim is a single statement — ``BEGIN IMMEDIATE`` (when the connection is
not already inside a caller-owned transaction) plus
``UPDATE ... WHERE status = 'pending' ... RETURNING *`` — so a second worker
racing for the same row claims nothing instead of relying on a separate
``SELECT`` plus ``rowcount``. Requires SQLite >= 3.35 (RETURNING); the repo
ships the CPython 3.11 runtime whose bundled SQLite is 3.45.1.

Fencing model: ``claim`` and ``reclaim_expired`` each bump
``lease_generation``; the row's current ``worker_id`` plus ``status =
'running'`` guard every write. A worker whose lease was reclaimed (worker
cleared, generation bumped) therefore cannot commit a late write: its
``complete``/``fail``/``heartbeat`` updates match zero rows and return
``False`` without mutating anything.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_JOBS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','completed','failed','cancelled')),
    worker_id TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after TIMESTAMP,
    error TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    heartbeat_at TIMESTAMP,
    completed_at TIMESTAMP
)
"""

# Claim path: oldest pending row per queue. run_after gates retry-delay
# delivery (a requeued row is not claimable until its delay elapses).
_JOBS_CLAIM_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_jobs_claim
ON jobs(queue, status, run_after, id)
"""

# Janitor path: expired running leases only (partial index keeps it tiny).
_JOBS_HEARTBEAT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_jobs_expired_lease
ON jobs(status, heartbeat_at) WHERE status = 'running'
"""

# One literal, fully parameterized: the max_attempts pair is NULL when the
# lease is uncapped, so the CASE branches collapse to a plain reclaim.
_RECLAIM_SQL = """
UPDATE jobs
SET status = CASE
        WHEN ? IS NOT NULL AND attempts >= ? THEN 'failed'
        ELSE 'pending'
    END,
    error = CASE
        WHEN ? IS NOT NULL AND attempts >= ?
            THEN COALESCE(NULLIF(error, ''), 'lease_attempt_cap_exceeded')
        ELSE error
    END,
    worker_id = NULL,
    lease_generation = lease_generation + 1,
    started_at = NULL,
    heartbeat_at = NULL
WHERE status = 'running'
  AND heartbeat_at IS NOT NULL
  AND heartbeat_at < datetime('now', ?)
  AND queue = COALESCE(?, queue)
"""


def ensure_jobs_schema(conn: sqlite3.Connection) -> None:
    """Create the shared ``jobs`` table and its indexes if absent.

    Idempotent (``IF NOT EXISTS``) and safe to run on every boot; commits only
    when it opened the transaction itself.
    """
    statements = (
        _JOBS_TABLE_DDL,
        _JOBS_CLAIM_INDEX_DDL,
        _JOBS_HEARTBEAT_INDEX_DDL,
    )
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            conn.execute(statement)
    except BaseException:
        if owned:
            conn.rollback()
        raise
    if owned:
        conn.commit()


class JobLease:
    """Claim/heartbeat/fence/janitor operations over the shared ``jobs`` table.

    ``max_attempts=None`` (the default) leaves reclaim uncapped — a lease that
    keeps expiring cycles back to ``pending`` forever, which is what the
    frozen acceptance checks pin. Callers that want the durability tax capped
    (the ingestion janitor passes ``settings.jobs_max_attempts``) set it here;
    a reclaim of a row at/over the cap then settles it terminally as
    ``failed`` with error ``lease_attempt_cap_exceeded``.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        reclaim_timeout_seconds: float = 300.0,
        max_attempts: Optional[int] = None,
    ) -> None:
        self._conn = conn
        self._reclaim_timeout_seconds = float(reclaim_timeout_seconds)
        self._max_attempts = max_attempts

    @contextmanager
    def _write_txn(self) -> Iterator[bool]:
        """``BEGIN IMMEDIATE`` when we hold the connection's transaction.

        Production pool connections run autocommit and test connections run
        default isolation; a caller that already has a transaction open keeps
        ownership (the single-statement writes below are atomic regardless).
        Yields whether WE own the transaction (and therefore commit it).
        """
        if self._conn.in_transaction:
            yield False
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield True
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def enqueue(
        self,
        queue: str,
        payload: dict[str, Any],
        run_after: Optional[str] = None,
    ) -> int:
        """Insert one pending job; returns its id."""
        with self._write_txn() as owned:
            cur = self._conn.execute(
                "INSERT INTO jobs (queue, payload_json, run_after) VALUES (?, ?, ?)",
                (queue, json.dumps(payload), run_after),
            )
            job_id = int(cur.lastrowid)
            del owned
        return job_id

    def claim(self, queue: str, worker_id: str) -> Optional[sqlite3.Row]:
        """Atomically claim the oldest claimable pending row of ``queue``.

        Returns the claimed row (``status='running'``, this ``worker_id``,
        generation bumped, heartbeat fresh) or ``None`` when nothing is
        claimable. Never returns a row another worker holds: the single
        statement only flips a still-``pending`` row.
        """
        with self._write_txn():
            cur = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    worker_id = ?,
                    lease_generation = lease_generation + 1,
                    attempts = attempts + 1,
                    started_at = CURRENT_TIMESTAMP,
                    heartbeat_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE queue = ?
                      AND status = 'pending'
                      AND (run_after IS NULL OR run_after <= CURRENT_TIMESTAMP)
                    ORDER BY id ASC
                    LIMIT 1
                )
                AND status = 'pending'
                RETURNING *
                """,
                (worker_id, queue),
            )
            row = cur.fetchone()
        return row

    def heartbeat(self, job_id: int, worker_id: str) -> bool:
        """Renew the lease if (and only if) ``worker_id`` still holds it."""
        with self._write_txn():
            cur = self._conn.execute(
                "UPDATE jobs SET heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (job_id, worker_id),
            )
            renewed = cur.rowcount > 0
        return renewed

    def complete(
        self,
        job_id: int,
        worker_id: str,
        result: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Fenced terminal success; ``False`` (and no mutation) when the
        lease was lost or already settled."""
        payload = json.dumps(result if result is not None else {})
        with self._write_txn():
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'completed', result_json = ?, "
                "completed_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (payload, job_id, worker_id),
            )
            done = cur.rowcount > 0
        return done

    def fail(self, job_id: int, worker_id: str, error: str) -> bool:
        """Fenced terminal failure; ``False`` (and no mutation) when the
        lease was lost or already settled."""
        with self._write_txn():
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, "
                "completed_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (error, job_id, worker_id),
            )
            done = cur.rowcount > 0
        return done

    def requeue(
        self,
        job_id: int,
        worker_id: str,
        delay_seconds: Optional[float] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Fenced retryable-failure requeue: back to ``pending`` with the
        attempts ledger intact. ``delay_seconds`` gates the next claim via
        ``run_after``, computed by SQLite's own clock so the stored format
        matches the claim filter's ``CURRENT_TIMESTAMP`` comparison (a Python
        ISO string with 'T'/timezone would never compare <= CURRENT_TIMESTAMP).
        """
        offset = (
            f"+{float(delay_seconds)} seconds"
            if delay_seconds is not None
            else None
        )
        with self._write_txn():
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'pending', worker_id = NULL, "
                "error = ?, "
                "run_after = CASE WHEN ? IS NULL THEN NULL "
                "ELSE datetime('now', ?) END, "
                "heartbeat_at = NULL "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (error, offset, offset, job_id, worker_id),
            )
            requeued = cur.rowcount > 0
        return requeued

    def release(self, job_id: int, worker_id: str) -> bool:
        """Fenced graceful-shutdown release: give the job up immediately
        (back to ``pending``) instead of waiting out the reclaim timeout."""
        with self._write_txn():
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'pending', worker_id = NULL, "
                "lease_generation = lease_generation + 1, heartbeat_at = NULL "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (job_id, worker_id),
            )
            released = cur.rowcount > 0
        return released

    def reclaim_expired(self, queue: Optional[str] = None) -> int:
        """Janitor pass: settle every expired lease in scope.

        Rows whose ``heartbeat_at`` is older than the reclaim timeout go back
        to ``pending`` (generation bumped, worker cleared) — or terminally to
        ``failed`` when this lease was constructed with ``max_attempts`` and
        the row is at/over the cap. Returns the number of rows settled.
        """
        cutoff = f"-{self._reclaim_timeout_seconds} seconds"
        with self._write_txn():
            cur = self._conn.execute(
                _RECLAIM_SQL,
                (
                    self._max_attempts,
                    self._max_attempts,
                    self._max_attempts,
                    self._max_attempts,
                    cutoff,
                    queue,
                ),
            )
            settled = cur.rowcount
        if settled:
            logger.debug(
                "JobLease: reclaimed %d expired lease(s)%s",
                settled,
                f" in queue {queue}" if queue else "",
            )
        return settled
