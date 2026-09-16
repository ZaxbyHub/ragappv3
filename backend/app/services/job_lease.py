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

# One literal per table shape, fully parameterized: the max_attempts pair is
# NULL when the lease is uncapped, so the CASE branches collapse to a plain
# reclaim. Only allowlisted table/column fragments are interpolated. The
# {error_col} fragment is the table's single error column ("error" on jobs;
# "error_code" on draft_jobs, which splits error into code+message).
_RECLAIM_SQL_TEMPLATE = """
UPDATE {table}
SET status = CASE
        WHEN ? IS NOT NULL AND attempts >= ? THEN 'failed'
        ELSE 'pending'
    END,
    {error_col} = CASE
        WHEN ? IS NOT NULL AND attempts >= ?
            THEN COALESCE(NULLIF({error_col}, ''), 'lease_attempt_cap_exceeded')
        ELSE {error_col}
    END,
    worker_id = NULL,
    lease_generation = lease_generation + 1,
    {reclaim_started_at_set},
    heartbeat_at = NULL
WHERE status = 'running'
  AND heartbeat_at IS NOT NULL
  AND heartbeat_at < datetime('now', ?)
  AND {queue_column} = COALESCE(?, {queue_column})
"""


def _reclaim_sql(shape: _TableShape, table: str) -> str:
    return _RECLAIM_SQL_TEMPLATE.format(
        table=table,
        queue_column=shape.queue_column,
        error_col="error_code" if shape.error_set.startswith("error_code") else "error",
        reclaim_started_at_set=shape.reclaim_started_at_set,
    )


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


class _TableShape:
    """Column-shape facts the SQL builder may interpolate, per allowlisted table.

    Everything here is a closed set validated in ``JobLease.__init__`` BEFORE
    any SQL string is built; no user-supplied text ever reaches a statement.
    ``error_set`` is the fenced failure write: ``jobs`` carries one ``error``
    column, ``draft_jobs`` splits it into ``error_code``/``error_message``.
    ``enqueue``/``requeue(delay_seconds=...)`` need ``payload_json``/``run_after``
    respectively, which only the ``jobs`` shape has — draft rows are enqueued
    and retried by ``DraftStore``'s native typed columns instead.
    """

    def __init__(
        self,
        *,
        queue_column: str,
        has_run_after: bool,
        error_set: str,
        supports_enqueue: bool,
        reclaim_started_at_set: str = "started_at = NULL",
    ) -> None:
        self.queue_column = queue_column
        self.has_run_after = has_run_after
        self.error_set = error_set
        self.supports_enqueue = supports_enqueue
        self.reclaim_started_at_set = reclaim_started_at_set


# The table allowlist (issue #559 stage 4, plan-critic MAJOR-2: one primitive,
# parameterized by target table — no parallel draft-specific claim path).
# Extending this map to a third table is a deliberate act: each entry must
# carry the full column shape and the acceptance-check family must be extended
# with it.
_TABLE_SHAPES: dict[str, _TableShape] = {
    "jobs": _TableShape(
        queue_column="queue",
        has_run_after=True,
        error_set="error = ?,",
        supports_enqueue=True,
    ),
    "draft_jobs": _TableShape(
        queue_column="job_type",
        has_run_after=False,
        error_set="error_code = ?,",
        supports_enqueue=False,
        # Deadline continuation (issue #516 DRAFT-013): a reclaim must NOT
        # clear started_at — the resumed run keeps burning the original
        # wall-clock budget instead of being re-granted a full timeout.
        reclaim_started_at_set="started_at = started_at",
    ),
}

# Claim ordering and claim-time started_at are allowlisted fragments, not
# free-form SQL.
_CLAIM_ORDERS = ("id ASC", "created_at ASC, id ASC")
_CLAIM_STARTED_AT_MODES = (
    "CURRENT_TIMESTAMP",
    "COALESCE(started_at, CURRENT_TIMESTAMP)",
)


class JobLease:
    """Claim/heartbeat/fence/janitor operations over the shared ``jobs`` table
    (or, parameterized, the ``draft_jobs`` table — same predicates, same
    fencing).

    ``max_attempts=None`` (the default) leaves reclaim uncapped — a lease that
    keeps expiring cycles back to ``pending`` forever. Callers that want the
    durability tax capped (the ingestion janitor passes
    ``settings.jobs_max_attempts``) set it here; a reclaim of a row at/over
    the cap then settles it terminally as ``failed`` with error
    ``lease_attempt_cap_exceeded``.

    The stage-2/4 parameters (all validated against closed allowlists, so no
    caller text reaches the SQL): ``table`` selects the physical table;
    ``claim_order`` preserves each queue's documented claim ordering (draft
    claims oldest-``created_at`` first, ties by id, exactly as the pre-lease
    SELECT ordered them); ``claim_started_at`` lets the draft compile claim
    keep a recovered job's original ``started_at`` (deadline continuation,
    issue #516 DRAFT-013) while every other claim stamps a fresh one.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        reclaim_timeout_seconds: float = 300.0,
        max_attempts: Optional[int] = None,
        table: str = "jobs",
        claim_order: str = "id ASC",
        claim_started_at: str = "CURRENT_TIMESTAMP",
    ) -> None:
        shape = _TABLE_SHAPES.get(table)
        if shape is None:
            raise ValueError(
                f"JobLease: unsupported table {table!r}; "
                f"allowed: {sorted(_TABLE_SHAPES)}"
            )
        if claim_order not in _CLAIM_ORDERS:
            raise ValueError(
                f"JobLease: unsupported claim_order {claim_order!r}; "
                f"allowed: {list(_CLAIM_ORDERS)}"
            )
        if claim_started_at not in _CLAIM_STARTED_AT_MODES:
            raise ValueError(
                f"JobLease: unsupported claim_started_at {claim_started_at!r}"
            )
        self._conn = conn
        self._reclaim_timeout_seconds = float(reclaim_timeout_seconds)
        self._max_attempts = max_attempts
        self._table = table
        self._shape = shape
        self._claim_order = claim_order
        self._claim_started_at = claim_started_at

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
        if not self._shape.supports_enqueue:
            raise ValueError(
                f"JobLease.enqueue: table {self._table!r} has no "
                "payload_json column; rows there are enqueued by their "
                "own typed store"
            )
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
        run_after_clause = (
            "  AND (run_after IS NULL OR run_after <= CURRENT_TIMESTAMP)\n"
            if self._shape.has_run_after
            else ""
        )
        sql = f"""
                UPDATE {self._table}
                SET status = 'running',
                    worker_id = ?,
                    lease_generation = lease_generation + 1,
                    attempts = attempts + 1,
                    started_at = {self._claim_started_at},
                    heartbeat_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id FROM {self._table}
                    WHERE {self._shape.queue_column} = ?
                      AND status = 'pending'
{run_after_clause}                    ORDER BY {self._claim_order}
                    LIMIT 1
                )
                AND status = 'pending'
                RETURNING *
                """  # nosec B608 - fragments are closed allowlist values
        with self._write_txn():
            cur = self._conn.execute(sql, (worker_id, queue))
            row = cur.fetchone()
        return row

    def attempts_of(self, job_id: int) -> Optional[int]:
        """Read a row's durable attempt counter (``None`` if the row is gone)."""
        row = self._conn.execute(
            f"SELECT attempts FROM {self._table} WHERE id = ?",  # nosec B608 - table is an allowlist value
            (job_id,),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def heartbeat(self, job_id: int, worker_id: str) -> bool:
        """Renew the lease if (and only if) ``worker_id`` still holds it."""
        with self._write_txn():
            cur = self._conn.execute(
                f"UPDATE {self._table} SET heartbeat_at = CURRENT_TIMESTAMP "  # nosec B608 - table is an allowlist value
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
                f"UPDATE {self._table} SET status = 'completed', result_json = ?, "  # nosec B608 - table is an allowlist value
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
                f"UPDATE {self._table} SET status = 'failed', {self._shape.error_set} "  # nosec B608 - allowlist fragments only
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
        if delay_seconds is not None and not self._shape.has_run_after:
            raise ValueError(
                f"JobLease.requeue: table {self._table!r} has no run_after "
                "column; requeue it without a delay and let its own store "
                "record retry state"
            )
        offset = (
            f"+{float(delay_seconds)} seconds"
            if delay_seconds is not None
            else None
        )
        run_after_set = (
            "run_after = CASE WHEN ? IS NULL THEN NULL "
            "ELSE datetime('now', ?) END, "
            if self._shape.has_run_after
            else ""
        )
        with self._write_txn():
            cur = self._conn.execute(
                f"UPDATE {self._table} SET status = 'pending', worker_id = NULL, "  # nosec B608 - allowlist fragments only
                f"{self._shape.error_set} "
                f"{run_after_set}"
                "heartbeat_at = NULL "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (error, offset, offset, job_id, worker_id)
                if self._shape.has_run_after
                else (error, job_id, worker_id),
            )
            requeued = cur.rowcount > 0
        return requeued

    def release(self, job_id: int, worker_id: str) -> bool:
        """Fenced graceful-shutdown release: give the job up immediately
        (back to ``pending``) instead of waiting out the reclaim timeout."""
        with self._write_txn():
            cur = self._conn.execute(
                f"UPDATE {self._table} SET status = 'pending', worker_id = NULL, "  # nosec B608 - table is an allowlist value
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
                _reclaim_sql(self._shape, self._table),
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
                f" in {self._shape.queue_column} {queue}" if queue else "",
            )
        return settled
