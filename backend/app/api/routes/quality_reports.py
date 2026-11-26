"""User-facing quality reports and replayable evaluation cases (issue #237).

PRODUCT-ENH-12: a structured quality report (incorrect answer / missing
source / stale source / bad extraction) is bound to the immutable message
identity and provenance available at HEAD — session id, message id, the
chat turn id + seq, a config/release snapshot reference, and the file
hashes of the sources persisted with the message. An operator converts a
report into a replayable evaluation case (query + expected outcome +
provenance) and scores before/after replays through the registered
evaluator semantics, closing the report -> case -> before/after loop.

Authz mirrors ``set_message_feedback`` (vault write + owner-or-admin) for
the user-facing endpoints and gates the operator endpoints on
``require_admin_role``.
"""

import json
import logging
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    require_admin_role,
)
from app.config import settings
from app.limiter import limiter
from app.security import csrf_protect
from app.services.eval_metrics import citation_validity, fact_coverage
from app.utils.sqlite_errors import is_unique_violation

router = APIRouter()
logger = logging.getLogger(__name__)

# Report notes are provenance records, not prompt-input hot paths. A generous
# independent cap leaves room for two full accepted notes and their attributions;
# overflow is rejected explicitly rather than destroying submitted text.
QUALITY_REPORT_NOTE_MAX_CHARS = 1_000_000
_NOTE_ATTRIBUTION_RESERVE = len(" (submitted by user 999999)") + len(" || ")


def _note_body_limit() -> int:
    """Bound one accepted note so two notes plus attributions fit the cap."""
    return (QUALITY_REPORT_NOTE_MAX_CHARS - 2 * _NOTE_ATTRIBUTION_RESERVE) // 2

#: Structured report categories (issue #237). A generic thumbs-down counter
#: is insufficient — each category routes to a different repair path.
REPORT_CATEGORIES = (
    "incorrect_answer",
    "missing_source",
    "stale_source",
    "bad_extraction",
)

_release_id_cache: Dict[str, str] = {}


def _config_ref() -> str:
    """Non-empty config/release snapshot reference for report provenance.

    Cached per process: the underlying git/env lookup is a subprocess we do
    not want per request. Only SUCCESSFUL lookups are cached (issue #800 AC4):
    ``_get_release_id`` returns ``None`` when no real id is available (git
    failed AND no env override), so the ``if value:`` guard below skips the
    cache and the next call retries — a transient git failure is never cached
    for the life of the process. This wrapper still returns a non-empty marker
    ('unknown') for the current call so provenance is never blank.
    """
    if "value" in _release_id_cache:
        return _release_id_cache["value"]
    from app.services.eval_adapter import _get_release_id

    value = _get_release_id()
    if value:
        _release_id_cache["value"] = value
        return value
    return "unknown"


def _source_file_hashes(raw_sources: Optional[str]) -> List[str]:
    """Sorted unique ``file_hash`` values of a message's persisted sources JSON.

    Malformed JSON yields ``[]``; entries without a ``file_hash`` are
    skipped — provenance extraction never errors (issue #237, critic R3).
    """
    if not raw_sources:
        return []
    try:
        parsed = json.loads(raw_sources)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return sorted(
        {
            entry["file_hash"]
            for entry in parsed
            if isinstance(entry, dict) and entry.get("file_hash")
        }
    )


class QualityReportRequest(BaseModel):
    """Body for POST /quality/reports."""

    session_id: int
    message_id: int
    category: str
    note: Optional[str] = None

    @field_validator("note")
    @classmethod
    def validate_note_separator(cls, value: Optional[str]) -> Optional[str]:
        """Reserve the separator and bound the note against the live cap."""
        if value is None:
            return value
        if " || " in value:
            raise ValueError("note must not contain the reserved separator ' || '")
        if len(value) > _note_body_limit():
            raise ValueError(
                f"note exceeds the live bound of {_note_body_limit()} characters"
            )
        return value


class ConvertReportRequest(BaseModel):
    """Body for POST /quality/reports/{report_id}/convert."""

    expected_outcome: str = Field(..., min_length=1)


class EvalCaseSide(BaseModel):
    """One side (before or after) of a replay comparison."""

    answer: str = ""
    cited_source_labels: List[str] = Field(default_factory=list)
    retrieved_source_labels: List[str] = Field(default_factory=list)


class EvalCaseCompareRequest(BaseModel):
    """Body for POST /quality/eval-cases/{case_id}/compare."""

    before: EvalCaseSide
    after: EvalCaseSide


_REPORT_COLS = (
    "id",
    "session_id",
    "message_id",
    "category",
    "note",
    "turn_id",
    "seq",
    "config_ref",
    "source_file_hashes",
    "created_at",
)


def _merge_report_note(existing: str, incoming: str, submitter_id: int) -> str:
    """Fold a differing re-submitted note into the canonical note (#1201 AC3).

    Uses the repo's existing ` || ` note-convention and attributes only the
    newly submitted note; repeat submissions of the same note are no-ops.
    """
    if not incoming or incoming == existing:
        return existing
    parts = [part for part in (existing or "").split(" || ") if part.strip()]
    cap = QUALITY_REPORT_NOTE_MAX_CHARS
    suffix = f" (submitted by user {submitter_id})"
    attributed = f"{incoming}{suffix}"
    if attributed in parts:
        return existing or ""
    parts.insert(0, attributed)
    return _fit_note_parts(parts, cap)


def _fit_note_parts(parts, cap: int) -> str:
    """Return the full canonical note or reject overflow without changing it."""
    merged = " || ".join(parts)
    if len(merged) <= cap:
        return merged
    raise HTTPException(
        status_code=422,
        detail=(
            f"quality report note exceeds report-note cap ({cap} characters); "
            "submit a shorter note"
        ),
    )


def _merge_note_or_missing(conn, row_id: int, incoming: str, user_id: int) -> bool:
    """Return False when the canonical report vanished during the merge."""
    try:
        _merge_report_note_cas(conn, row_id, incoming, user_id)
        return True
    except LookupError:
        return False


def _merge_report_note_cas(conn, row_id: int, incoming: str, user_id: int) -> str:
    """Fold a note into a canonical row with compare-and-swap retries.

    A stale reader cannot overwrite a concurrent merge: the UPDATE carries the
    note value that was read, and a failed CAS re-reads before retrying.
    """
    for _ in range(8):
        result = conn.execute(
            "SELECT note FROM quality_reports WHERE id = ?", (row_id,)
        )
        row = result.fetchone()
        if row is None:
            raise LookupError(f"quality report {row_id} vanished during merge")
        old_value = row[0]  # keep SQL NULL intact for the comparison
        old = old_value or ""
        merged = _merge_report_note(old, incoming, user_id)
        if merged == old and (old_value is not None or not incoming):
            return old
        if old_value is None:
            update = conn.execute(
                "UPDATE quality_reports SET note = ? WHERE id = ? AND note IS NULL",
                (merged, row_id),
            )
        else:
            update = conn.execute(
                "UPDATE quality_reports SET note = ? WHERE id = ? AND note = ?",
                (merged, row_id, old_value),
            )
        if update.rowcount:
            conn.commit()
            return merged
    raise RuntimeError("quality report note merge lost too many races")


def _row_get(row: Any, col: str) -> Any:
    """Positional column access that works for both sqlite3.Row and tuple."""
    return row[_REPORT_COLS.index(col)] if col in _REPORT_COLS else row[col]


def _report_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": _row_get(row, "id"),
        "session_id": _row_get(row, "session_id"),
        "message_id": _row_get(row, "message_id"),
        "category": _row_get(row, "category"),
        "note": _row_get(row, "note"),
        "turn_id": _row_get(row, "turn_id"),
        "seq": _row_get(row, "seq"),
        "provenance": {
            "config_ref": _row_get(row, "config_ref"),
            "source_file_hashes": json.loads(
                _row_get(row, "source_file_hashes") or "[]"
            ),
        },
        "created_at": _row_get(row, "created_at"),
    }


async def _authorize_report_access(
    conn: sqlite3.Connection,
    session_id: int,
    user: dict,
    evaluate: Callable,
) -> None:
    """Session 404 -> vault-write 403 -> owner-or-admin 403 (feedback shape)."""
    import asyncio

    session_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, vault_id, user_id FROM chat_sessions WHERE id = ?",
        (session_id,),
    )
    session_row = await asyncio.to_thread(session_result.fetchone)
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await evaluate(user, "vault", session_row[1], "write"):
        raise HTTPException(status_code=403, detail="No write access to this vault")
    owner_id = session_row[2]
    if (
        owner_id is not None
        and owner_id != user["id"]
        and user.get("role") not in ("superadmin", "admin")
    ):
        raise HTTPException(
            status_code=403,
            detail="Cannot report quality issues for another user's session",
        )


@router.post("/quality/reports")
@limiter.limit(settings.chat_rate_limit)
async def submit_quality_report(
    request: Request,
    body: QualityReportRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """Submit a structured quality report bound to the message's turn identity."""
    import asyncio

    if body.category not in REPORT_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"category must be one of {list(REPORT_CATEGORIES)}",
        )

    await _authorize_report_access(conn, body.session_id, user, evaluate)

    msg_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, turn_id, seq, sources FROM chat_messages "
        "WHERE id = ? AND session_id = ?",
        (body.message_id, body.session_id),
    )
    msg_row = await asyncio.to_thread(msg_result.fetchone)
    if msg_row is None:
        raise HTTPException(status_code=404, detail="Message not found in this session")

    config_ref = await asyncio.to_thread(_config_ref)
    # msg_row columns (positional): 0=id, 1=turn_id, 2=seq, 3=sources
    file_hashes_json = json.dumps(_source_file_hashes(msg_row[3]))
    # Idempotent submit (issue #800 AC2): the retry identity is
    # (message_id, category) — a retried POST of the same report returns the
    # existing row; independent reports (different message or category) are
    # never suppressed (the September reconstruction constraint). The
    # pre-SELECT plus unique index close the concurrent window: a racing
    # winner that committed between the SELECT and the INSERT surfaces as
    # IntegrityError, and the recovery rolls back and re-selects the winner.
    existing_result = await asyncio.to_thread(
        conn.execute,
        "SELECT * FROM quality_reports "
        "WHERE message_id = ? AND category = ? ORDER BY id ASC LIMIT 1",
        (body.message_id, body.category),
    )
    existing_row = await asyncio.to_thread(existing_result.fetchone)
    if existing_row is not None:
        if not await asyncio.to_thread(
            _merge_note_or_missing,
            conn,
            _row_get(existing_row, "id"),
            body.note or "",
            user["id"],
        ):
            raise HTTPException(status_code=404, detail="Quality report not found")
        updated_result = await asyncio.to_thread(
            conn.execute,
            "SELECT * FROM quality_reports WHERE id = ?",
            (_row_get(existing_row, "id"),),
        )
        existing_row = await asyncio.to_thread(updated_result.fetchone)
        if existing_row is None:
            raise HTTPException(status_code=404, detail="Quality report not found")
        return _report_row_to_dict(existing_row)
    try:
        cursor = await asyncio.to_thread(
            conn.execute,
            "INSERT INTO quality_reports (session_id, message_id, category, note, "
            "turn_id, seq, config_ref, source_file_hashes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.session_id,
                body.message_id,
                body.category,
                body.note,
                msg_row[1],
                msg_row[2],
                config_ref,
                file_hashes_json,
            ),
        )
        await asyncio.to_thread(conn.commit)
    except sqlite3.IntegrityError as exc:
        await asyncio.to_thread(conn.rollback)
        if not is_unique_violation(exc):
            session_result = await asyncio.to_thread(
                conn.execute,
                "SELECT 1 FROM chat_sessions WHERE id = ?",
                (body.session_id,),
            )
            if await asyncio.to_thread(session_result.fetchone) is None:
                raise HTTPException(status_code=404, detail="Session not found")
            msg_result = await asyncio.to_thread(
                conn.execute,
                "SELECT 1 FROM chat_messages WHERE id = ? AND session_id = ?",
                (body.message_id, body.session_id),
            )
            if await asyncio.to_thread(msg_result.fetchone) is None:
                raise HTTPException(
                    status_code=404,
                    detail="Message not found in this session",
                )
            raise
        # A concurrent submission of the same identity won the race (unique
        # index). Roll the failed statement back and return the winner.
        winner_result = await asyncio.to_thread(
            conn.execute,
            "SELECT * FROM quality_reports "
            "WHERE message_id = ? AND category = ? ORDER BY id ASC LIMIT 1",
            (body.message_id, body.category),
        )
        winner_row = await asyncio.to_thread(winner_result.fetchone)
        if winner_row is None:
            raise
        if not await asyncio.to_thread(
            _merge_note_or_missing,
            conn,
            _row_get(winner_row, "id"),
            body.note or "",
            user["id"],
        ):
            raise HTTPException(status_code=404, detail="Quality report not found")
        updated_result = await asyncio.to_thread(
            conn.execute,
            "SELECT * FROM quality_reports WHERE id = ?",
            (_row_get(winner_row, "id"),),
        )
        winner_row = await asyncio.to_thread(updated_result.fetchone)
        if winner_row is None:
            raise HTTPException(status_code=404, detail="Quality report not found")
        row = winner_row
    else:
        row_result = await asyncio.to_thread(
            conn.execute,
            "SELECT * FROM quality_reports WHERE id = ?",
            (cursor.lastrowid,),
        )
        row = await asyncio.to_thread(row_result.fetchone)
    logger.info(
        "Quality report %s submitted (category=%s, session=%s, message=%s)",
        row[0],
        body.category,
        body.session_id,
        body.message_id,
    )
    return _report_row_to_dict(row)


@router.get("/quality/reports")
async def list_quality_reports(
    session_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """List the quality reports of one chat session (same authz family)."""
    import asyncio

    await _authorize_report_access(conn, session_id, user, evaluate)
    result = await asyncio.to_thread(
        conn.execute,
        "SELECT * FROM quality_reports WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    )
    rows = await asyncio.to_thread(result.fetchall)
    return {"reports": [_report_row_to_dict(row) for row in rows]}


@router.post("/quality/reports/{report_id}/convert")
@limiter.limit(settings.chat_rate_limit)
async def convert_report_to_eval_case(
    request: Request,
    report_id: int,
    body: ConvertReportRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_admin_role),
    _csrf_token: str = Depends(csrf_protect),
):
    """Convert a report into a replayable evaluation case (operator, admin)."""
    import asyncio

    report_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, session_id, message_id, turn_id, seq, config_ref, "
        "source_file_hashes FROM quality_reports WHERE id = ?",
        (report_id,),
    )
    report = await asyncio.to_thread(report_result.fetchone)
    if report is None:
        raise HTTPException(status_code=404, detail="Quality report not found")

    # Idempotent convert (issue #800 AC3): one case per report — a retried
    # convert returns the existing case; the unique index plus IntegrityError
    # recovery close the concurrent window the same way submit does.
    existing_case_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, report_id, query, expected_outcome, provenance "
        "FROM quality_eval_cases WHERE report_id = ? ORDER BY id ASC LIMIT 1",
        (report_id,),
    )
    existing_case = await asyncio.to_thread(existing_case_result.fetchone)
    if existing_case is not None:
        return {
            "id": existing_case[0],
            "report_id": existing_case[1],
            "query": existing_case[2],
            "expected_outcome": existing_case[3],
            "provenance": json.loads(existing_case[4] or "{}"),
        }

    # The case's query is the turn's user message. Primary match: the
    # stored turn_id. Fallback for legacy/migrated sessions whose rows carry
    # NULL turns (issue #800 AC1): strict seq adjacency — the user row at
    # the greatest seq strictly below the assistant row's seq with NO other
    # message row between them. Any other NULL-turn shape (no preceding user
    # row, a gap, an intervening row, or NULL ordering on either side) is
    # unresolvable ambiguity and is refused without inserting a case.
    query_row = None
    user_message_id: Optional[int] = None
    user_seq: Optional[int] = None
    if report[3] is not None:
        query_result = await asyncio.to_thread(
            conn.execute,
            "SELECT content, id, seq FROM chat_messages "
            "WHERE session_id = ? AND turn_id = ? AND role = 'user' "
            "ORDER BY seq ASC, id ASC LIMIT 1",
            (report[1], report[3]),
        )
        query_row = await asyncio.to_thread(query_result.fetchone)
        if query_row is not None:
            user_message_id = query_row[1]
            user_seq = query_row[2]
    else:
        report_seq_result = await asyncio.to_thread(
            conn.execute,
            "SELECT seq FROM chat_messages WHERE id = ?",
            (report[2],),
        )
        report_seq_row = await asyncio.to_thread(report_seq_result.fetchone)
        if report_seq_row is not None and report_seq_row[0] is not None:
            adjacency_result = await asyncio.to_thread(
                conn.execute,
                "SELECT content, id, seq FROM chat_messages "
                "WHERE session_id = ? AND role = 'user' AND seq < ? "
                "ORDER BY seq DESC, id DESC LIMIT 1",
                (report[1], report_seq_row[0]),
            )
            candidate = await asyncio.to_thread(adjacency_result.fetchone)
            if candidate is not None:
                # Refuse unless the candidate is NUMERICALLY adjacent: the
                # user row must sit at assistant_seq - 1. This is stricter
                # than counting intervening rows — a deleted middle row
                # leaves a numeric gap that is exactly the ambiguity the
                # plan refuses (05-fix-plan.md F12b: "Any other shape (gap,
                # intervening row...) -> 404 refusal, no insert").
                if candidate[2] == report_seq_row[0] - 1:
                    query_row = candidate
                    user_message_id = candidate[1]
                    user_seq = candidate[2]
    if query_row is None:
        raise HTTPException(
            status_code=404,
            detail="No user message found for the report's turn",
        )

    provenance = {
        "config_ref": report[5],
        "source_file_hashes": json.loads(report[6] or "[]"),
        "session_id": report[1],
        "message_id": report[2],
        "turn_id": report[3],
        "user_message_id": user_message_id,
        "user_seq": user_seq,
    }
    try:
        cursor = await asyncio.to_thread(
            conn.execute,
            "INSERT INTO quality_eval_cases (report_id, query, expected_outcome, "
            "provenance) VALUES (?, ?, ?, ?)",
            (
                report_id,
                query_row[0],
                body.expected_outcome,
                json.dumps(provenance, sort_keys=True),
            ),
        )
        await asyncio.to_thread(conn.commit)
    except sqlite3.IntegrityError as exc:
        await asyncio.to_thread(conn.rollback)
        if not is_unique_violation(exc):
            report_result = await asyncio.to_thread(
                conn.execute, "SELECT 1 FROM quality_reports WHERE id = ?", (report_id,)
            )
            if await asyncio.to_thread(report_result.fetchone) is None:
                raise HTTPException(status_code=404, detail="Quality report not found")
            raise
        # A concurrent convert of the same report won the race (unique
        # index). Roll back and return the winner's case.
        winner_case_result = await asyncio.to_thread(
            conn.execute,
            "SELECT id, report_id, query, expected_outcome, provenance "
            "FROM quality_eval_cases WHERE report_id = ? ORDER BY id ASC LIMIT 1",
            (report_id,),
        )
        winner_case = await asyncio.to_thread(winner_case_result.fetchone)
        if winner_case is None:
            raise
        return {
            "id": winner_case[0],
            "report_id": winner_case[1],
            "query": winner_case[2],
            "expected_outcome": winner_case[3],
            "provenance": json.loads(winner_case[4] or "{}"),
        }

    case_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, report_id, query, expected_outcome, provenance "
        "FROM quality_eval_cases WHERE id = ?",
        (cursor.lastrowid,),
    )
    case = await asyncio.to_thread(case_result.fetchone)
    logger.info("Quality report %s converted to eval case %s", report_id, case[0])
    return {
        "id": case[0],
        "report_id": case[1],
        "query": case[2],
        "expected_outcome": case[3],
        "provenance": json.loads(case[4] or "{}"),
    }


@router.post("/quality/eval-cases/{case_id}/compare")
@limiter.limit(settings.chat_rate_limit)
async def compare_eval_case(
    request: Request,
    case_id: int,
    body: EvalCaseCompareRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_admin_role),
    _csrf_token: str = Depends(csrf_protect),
):
    """Score before/after replays of a case through the evaluator semantics.

    fact_coverage = substring coverage of the case's expected outcome in the
    answer; citation_validity = cited labels valid against retrieved labels;
    delta = after - before. Pure, deterministic scoring on the registered
    metric functions — the same semantics the offline harness uses.
    """
    import asyncio

    case_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, expected_outcome FROM quality_eval_cases WHERE id = ?",
        (case_id,),
    )
    case = await asyncio.to_thread(case_result.fetchone)
    if case is None:
        raise HTTPException(status_code=404, detail="Evaluation case not found")

    expected = [case[1]]

    def _score(side: EvalCaseSide) -> Dict[str, float]:
        return {
            "fact_coverage": fact_coverage(side.answer, expected),
            "citation_validity": citation_validity(
                side.cited_source_labels, side.retrieved_source_labels
            ),
        }

    before = _score(body.before)
    after = _score(body.after)
    metrics = {
        name: {
            "before": before[name],
            "after": after[name],
            "delta": after[name] - before[name],
        }
        for name in ("fact_coverage", "citation_validity")
    }
    return {"case_id": case_id, "metrics": metrics}
