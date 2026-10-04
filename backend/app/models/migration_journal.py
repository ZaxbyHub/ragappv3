"""Migration/recovery journal for non-destructive storage recovery (issue #512).

Records schema version, migration phase, authoritative index generation, and
recovery outcome for the migrations owned by Workstream C1. Consumers:

- the repaired SQLite migrations (`app.models.database`) call
  ``record_migration_outcome`` at start/succeeded/failed/recovered phases and
  ``invalidate_derived_data`` when a repair rebuilds derived state (the
  explicit schema/claim-source invalidation interface for roadmap slots
  D1/D2);
- the vector-store staging swap (`app.services.vector_store`) calls
  ``publish_index_generation`` on every successful swap (the generation
  publication interface for slot C2);
- startup logs the latest outcomes so operators see recovery state.

This module deliberately keeps to stdlib sqlite3 so it can be called from any
connection a migration already holds.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# Bumped whenever the SCHEMA constant in app.models.database changes shape.
# 1 = pre-journal schema (the state before issue #512 added migration_journal).
MIGRATION_SCHEMA_VERSION = 2

MIGRATION_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS migration_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_name TEXT NOT NULL,
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
;
"""

_PHASES = ("start", "succeeded", "failed", "recovered")
_OUTCOMES = ("ok", "error", "recovered_from_backup", "rebuilt", "noop")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(MIGRATION_JOURNAL_DDL)


def record_migration_outcome(
    conn: sqlite3.Connection,
    *,
    migration_name: str,
    phase: str,
    outcome: str,
    detail: Optional[str] = None,
) -> None:
    """Append one journal row for a migration phase/outcome transition.

    Never raises: journaling must not take a recovering migration down. Rows
    commit independently of the caller's swap transaction (migrations run
    with ``isolation_level = None`` and journal outside ``BEGIN
    IMMEDIATE``), so a rolled-back swap still records its failure — the
    recovery journal must describe attempts, not only completions.
    """
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO migration_journal (migration_name, phase, outcome, detail)"
            " VALUES (?, ?, ?, ?)",
            (migration_name, phase, outcome, detail),
        )
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        logger.warning("migration_journal: could not record %s/%s: %s", migration_name, phase, exc)


def record_schema_version(
    conn: sqlite3.Connection,
    *,
    schema_version: int = MIGRATION_SCHEMA_VERSION,
) -> None:
    """Record the schema version the database is being brought to.

    Append-only per distinct version: re-running migrations at an unchanged
    version (every startup) adds no row, so the journal records version
    transitions rather than process starts.
    """
    detail = f"schema_version={schema_version}"
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT detail FROM migration_journal"
            " WHERE migration_name = 'schema' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is not None and row[0] == detail:
            return
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        logger.warning("migration_journal: could not read schema version: %s", exc)
    record_migration_outcome(
        conn,
        migration_name="schema",
        phase="succeeded",
        outcome="ok",
        detail=detail,
    )


def invalidate_derived_data(
    conn: sqlite3.Connection,
    *,
    reason: str,
    migration_name: str = "invalidate_derived_data",
) -> None:
    """Explicit schema/claim-source invalidation interface (slots D1/D2).

    Marks derived state (FTS rows, claim-source projections, cached schema
    views) as invalidated after a repair rebuilt authoritative rows. The row
    is a durable, operator-visible audit marker only: no automated consumer
    currently rebuilds derived state from this signal (issue #699 removed the
    earlier docstring claim to the contrary), so a reader must treat it as
    "derived state was rebuilt/invalidated by this migration", not as a
    trigger.
    """
    record_migration_outcome(
        conn,
        migration_name=migration_name,
        phase="succeeded",
        outcome="rebuilt",
        detail=f"invalidated_derived_data reason={reason}",
    )


def publish_index_generation(
    conn: sqlite3.Connection,
    *,
    store: str,
    table_name: str,
    detail: str,
) -> int:
    """Index-generation publication interface (slot C2).

    Called by the vector-store staging swap after the replacement table is
    validated. Returns the journal row id — the monotonically increasing
    authoritative generation for that (store, table).
    """
    _ensure_table(conn)
    cur = conn.execute(
        "INSERT INTO migration_journal (migration_name, phase, outcome, detail)"
        " VALUES (?, ?, ?, ?)",
        (f"index_generation:{store}:{table_name}", "succeeded", "ok", detail),
    )
    return int(cur.lastrowid or 0)


def latest_outcomes(sqlite_path: str, *, limit: int = 20) -> list[dict]:
    """Operator-visible tail of the journal (newest first)."""
    conn = sqlite3.connect(sqlite_path)
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, migration_name, phase, outcome, detail, created_at"
            " FROM migration_journal ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "migration_name": r[1],
                "phase": r[2],
                "outcome": r[3],
                "detail": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


def latest_outcomes_with_signal(
    sqlite_path: str, *, limit: int = 3
) -> tuple[list[dict], Optional[dict]]:
    """Startup-summary composition (issue #699): the unchanged newest-first
    ``latest_outcomes`` window, plus — only when that window contains no
    failed/recovered row — the newest failure-or-recovery row from the whole
    journal (else ``None``). This keeps a genuine failure visible in the
    startup summary even on databases that still carry routine per-boot noise
    rows written before that noise was eliminated, without changing
    ``latest_outcomes``' ordering contract.
    """
    recent = latest_outcomes(sqlite_path, limit=limit)
    if any(r.get("phase") in ("failed", "recovered") for r in recent):
        return recent, None
    conn = sqlite3.connect(sqlite_path)
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT id, migration_name, phase, outcome, detail, created_at"
            " FROM migration_journal WHERE phase IN ('failed', 'recovered')"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return recent, None
    return recent, {
        "id": row[0],
        "migration_name": row[1],
        "phase": row[2],
        "outcome": row[3],
        "detail": row[4],
        "created_at": row[5],
    }
