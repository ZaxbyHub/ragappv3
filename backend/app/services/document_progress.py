"""Phase-aware processing-progress helpers for the `files` table.

Status (`files.status`) stays in the canonical 4-value enum
('pending','processing','indexed','error'). All async lifecycle detail
(queued / parsing / chunking / embedding / writing-index / wiki) lives in
the new `phase` column and friends. Frontend polls
`GET /documents/{file_id}/status` and uses these fields to render
phase-aware progress without ever conflating upload completion with
indexing completion.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from app.models.database import SQLiteConnectionPool

logger = logging.getLogger(__name__)


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
    }
)


def set_phase(
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
    (embedding loops) don't pin pool capacity. Writes are best-effort: a
    progress-update failure must never abort indexing.

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
        with pool.connection() as conn:
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


def clear_progress(pool: SQLiteConnectionPool, file_id: int) -> None:
    """Reset transient progress fields on terminal success.

    Called after a successful indexing run so the next poll snapshot shows
    a clean ``indexed`` state without stale processed/total counters.
    `phase` is left at ``indexed`` so the frontend can distinguish "ready"
    from "still mid-pipeline".
    """
    try:
        with pool.connection() as conn:
            conn.execute(
                """
                UPDATE files
                SET phase = ?,
                    phase_message = NULL,
                    progress_percent = NULL,
                    processed_units = NULL,
                    total_units = NULL,
                    unit_label = NULL,
                    phase_started_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (PHASE_INDEXED, file_id),
            )
            conn.commit()
    except (sqlite3.Error, RuntimeError) as e:  # pragma: no cover - defensive
        # RuntimeError = expected pool-checkout failure (issue #513 W2).
        logger.warning("clear_progress failed for file_id=%s: %s", file_id, e)


def set_wiki_pending(
    pool: SQLiteConnectionPool, file_id: int, pending: bool
) -> None:
    """Set or clear the wiki_pending flag synchronously.

    Set TRUE when DocumentProcessor is about to enqueue the wiki ingest job
    so the status route can report ``wiki_status="pending"`` for the brief
    window before any wiki_compile_jobs row exists. Cleared once the job
    row appears (route-side derivation also handles the cleared state by
    falling back to the latest jobs row).
    """
    try:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE files SET wiki_pending = ? WHERE id = ?",
                (1 if pending else 0, file_id),
            )
            conn.commit()
    except (sqlite3.Error, RuntimeError) as e:  # pragma: no cover - defensive
        # RuntimeError = expected pool-checkout failure (issue #513 W2).
        logger.warning("set_wiki_pending failed for file_id=%s: %s", file_id, e)
