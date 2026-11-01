"""Phase-aware processing-progress helpers for the `files` table.

Status (`files.status`) stays in the canonical 6-value enum
('pending','processing','indexed','partial','error','cancelled'). All
async lifecycle detail
(queued / parsing / chunking / embedding / writing-index / wiki) lives in
the new `phase` column and friends. Frontend polls
`GET /documents/{file_id}/status` and uses these fields to render
phase-aware progress without ever conflating upload completion with
indexing completion.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from app.models.database import SQLiteConnectionPool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def write_session(
    pool: SQLiteConnectionPool, permit: Optional[object] = None
) -> AsyncIterator[sqlite3.Connection]:
    """Yield a pooled connection under the shared SQLite write permit.

    Single safe pattern for every status/commit write (issue #513 W1/RC-3,
    extended by issue #704 T1-25-KR-09 to the module-level progress helpers):
    the permit is acquired FIRST, but the fallible pool checkout happens
    INSIDE the region protected by the permit's finally, so a
    ``get_connection`` failure (RuntimeError on exhaustion/closed pool) can
    never leak the permit. The connection is released in a nested finally
    before the permit is released in the outer finally.

    ``permit`` defaults to the pool-carried ``write_permit`` attribute when
    the caller does not pass one: the BackgroundProcessor that owns the
    semaphore installs it on the pool at startup, so every module-level
    helper shares the exact permit the DocumentProcessor's ``_write_session``
    uses. ``None`` (no BackgroundProcessor / tests / sync routes) means no
    serialization, matching the pre-#704 behavior of those contexts.
    """
    if permit is None:
        # Instance-dict probe, not getattr: the attribute must have been
        # ACTUALLY SET on the pool (BackgroundProcessor does that at startup).
        # getattr would let mock pools auto-fabricate a non-awaitable
        # "write_permit" and crash the acquire below.
        permit = (
            pool.__dict__.get("write_permit") if hasattr(pool, "__dict__") else None
        )
    if permit is not None:
        await permit.acquire()
    try:
        conn = await pool.get_connection_async()
        try:
            yield conn
        finally:
            pool.release_connection(conn)
    finally:
        if permit is not None:
            permit.release()


# Canonical phase strings emitted by DocumentProcessor. The frontend maps
# these to user-facing labels; preserving the wire values means backend
# changes don't silently desync UX.
PHASE_QUEUED = "queued"
PHASE_PARSING = "parsing"
PHASE_EXTRACTING_TEXT = "extracting_text"
PHASE_CHUNKING = "chunking"
PHASE_EMBEDDING = "embedding"
PHASE_WRITING_INDEX = "writing_index"
PHASE_INDEXED = "indexed"
PHASE_ERROR = "error"
# Terminal phase of a user-cancelled ingest (issue #783): the cancel route
# flips files.status to 'cancelled' and the worker's unwind clears transient
# counters while landing this phase.
PHASE_CANCELLED = "cancelled"

ALL_PHASES = frozenset(
    {
        PHASE_QUEUED,
        PHASE_PARSING,
        PHASE_EXTRACTING_TEXT,
        PHASE_CHUNKING,
        PHASE_EMBEDDING,
        PHASE_WRITING_INDEX,
        PHASE_INDEXED,
        PHASE_ERROR,
        PHASE_CANCELLED,
    }
)


async def set_phase(
    pool: SQLiteConnectionPool,
    file_id: int,
    *,
    phase: Optional[str] = None,
    message: Optional[str] = None,
    percent: Optional[float] = None,
    processed: Optional[int] = None,
    total: Optional[int] = None,
    unit: Optional[str] = None,
    mark_processing_started: bool = False,
) -> None:
    """Atomically update phase-aware progress fields on a `files` row.

    Acquires and releases a pool connection per call so long-running phases
    (embedding loops) don't pin pool capacity. The checkout runs off the
    event loop via the pool's dedicated checkout executor (#645). Writes are
    best-effort: a progress-update failure must never abort indexing.

    Only fields explicitly provided are written; unset fields preserve
    their prior value. Passing ``phase`` updates ``phase_started_at`` only
    when the phase actually transitions (read-modify-write inside a single
    connection so two callers can't race the timestamp).
    """
    if phase is not None and phase not in ALL_PHASES:
        logger.warning("set_phase: unknown phase %r — writing anyway", phase)

    sets: list[str] = []
    params: list[Any] = []

    try:
        async with write_session(pool) as conn:
            current_phase: Optional[str] = None
            if phase is not None:
                row = conn.execute(
                    "SELECT phase FROM files WHERE id = ?", (file_id,)
                ).fetchone()
                if row is not None:
                    # sqlite3.Row supports indexing by name; tolerate tuple too
                    try:
                        current_phase = row["phase"]
                    except (TypeError, IndexError):
                        current_phase = row[0] if len(row) else None
                sets.append("phase = ?")
                params.append(phase)
                if phase != current_phase:
                    sets.append("phase_started_at = CURRENT_TIMESTAMP")
            if message is not None:
                sets.append("phase_message = ?")
                params.append(message)
            if percent is not None:
                # Clamp; an out-of-range percent shouldn't poison the row
                clamped = max(0.0, min(100.0, float(percent)))
                sets.append("progress_percent = ?")
                params.append(clamped)
            if processed is not None:
                sets.append("processed_units = ?")
                params.append(int(processed))
            if total is not None:
                sets.append("total_units = ?")
                params.append(int(total))
            if unit is not None:
                sets.append("unit_label = ?")
                params.append(unit)
            if mark_processing_started:
                # Set processing_started_at only on first transition to processing.
                sets.append(
                    "processing_started_at = COALESCE(processing_started_at, CURRENT_TIMESTAMP)"
                )

            if not sets:
                return

            params.append(file_id)
            conn.execute(
                f"UPDATE files SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            conn.commit()
    except (sqlite3.Error, RuntimeError) as e:  # pragma: no cover - defensive
        # RuntimeError = expected pool-checkout failure (exhaustion/closed pool,
        # database.py) — best-effort contract absorbs it (issue #513 W2).
        # CancelledError (BaseException) and programming errors still propagate.
        logger.warning("set_phase failed for file_id=%s: %s", file_id, e)


async def clear_progress(
    pool: SQLiteConnectionPool,
    file_id: int,
    *,
    phase: str = PHASE_INDEXED,
    phase_message: Optional[str] = None,
) -> None:
    """Reset transient progress fields on terminal success.

    Called after a successful indexing run so the next poll snapshot shows
    a clean ``indexed`` state without stale processed/total counters.
    `phase` is left at ``indexed`` so the frontend can distinguish "ready"
    from "still mid-pipeline". The checkout runs off the event loop via the
    pool's dedicated checkout executor (#645).

    Issue #783 adds the keyword-only ``phase``/``phase_message`` overrides so
    a cancelled ingest can land its terminal phase (``PHASE_CANCELLED``) and
    a user-facing note in the same single write; the defaults preserve the
    historical behavior byte-for-byte (indexed + message cleared).
    """
    try:
        async with write_session(pool) as conn:
            conn.execute(
                """
                UPDATE files
                SET phase = ?,
                    phase_message = ?,
                    progress_percent = NULL,
                    processed_units = NULL,
                    total_units = NULL,
                    unit_label = NULL,
                    phase_started_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (phase, phase_message, file_id),
            )
            conn.commit()
    except (sqlite3.Error, RuntimeError) as e:  # pragma: no cover - defensive
        # RuntimeError = expected pool-checkout failure (exhaustion/closed pool,
        # database.py) — best-effort contract absorbs it (issue #513 W2).
        logger.warning("clear_progress failed for file_id=%s: %s", file_id, e)


async def set_wiki_pending(
    pool: SQLiteConnectionPool, file_id: int, pending: bool
) -> None:
    """Set or clear the wiki_pending flag.

    Set TRUE when DocumentProcessor is about to enqueue the wiki ingest job
    so the status route can report ``wiki_status="pending"`` for the brief
    window before any wiki_compile_jobs row exists. Cleared once the job
    row appears (route-side derivation also handles the cleared state by
    falling back to the latest jobs row). The checkout runs off the event
    loop via the pool's dedicated checkout executor (#645).
    """
    try:
        async with write_session(pool) as conn:
            conn.execute(
                "UPDATE files SET wiki_pending = ? WHERE id = ?",
                (1 if pending else 0, file_id),
            )
            conn.commit()
    except (sqlite3.Error, RuntimeError) as e:  # pragma: no cover - defensive
        # RuntimeError = expected pool-checkout failure (issue #513 W2).
        logger.warning("set_wiki_pending failed for file_id=%s: %s", file_id, e)
