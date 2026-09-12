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
from pydantic import BaseModel, Field

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

router = APIRouter()
logger = logging.getLogger(__name__)

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
    not want per request. Never empty (falls back through env vars to a
    literal 'unknown' marker).
    """
    if "value" in _release_id_cache:
        return _release_id_cache["value"]
    from app.services.eval_adapter import _get_release_id

    value = _get_release_id() or "unknown"
    _release_id_cache["value"] = value
    return value


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
    "id", "session_id", "message_id", "category", "note", "turn_id", "seq",
    "config_ref", "source_file_hashes", "created_at",
)


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
            "source_file_hashes": json.loads(_row_get(row, "source_file_hashes") or "[]"),
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
        raise HTTPException(
            status_code=404, detail="Message not found in this session"
        )

    config_ref = _config_ref()
    # msg_row columns (positional): 0=id, 1=turn_id, 2=seq, 3=sources
    file_hashes_json = json.dumps(_source_file_hashes(msg_row[3]))
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

    row_result = await asyncio.to_thread(
        conn.execute,
        "SELECT * FROM quality_reports WHERE id = ?",
        (cursor.lastrowid,),
    )
    row = await asyncio.to_thread(row_result.fetchone)
    logger.info(
        "Quality report %s submitted (category=%s, session=%s, message=%s)",
        row[0], body.category, body.session_id, body.message_id,
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
        "SELECT id, session_id, message_id, turn_id, config_ref, "
        "source_file_hashes FROM quality_reports WHERE id = ?",
        (report_id,),
    )
    report = await asyncio.to_thread(report_result.fetchone)
    if report is None:
        raise HTTPException(status_code=404, detail="Quality report not found")

    # The case's query is the turn's user message (matched by turn_id).
    query_result = await asyncio.to_thread(
        conn.execute,
        "SELECT content FROM chat_messages "
        "WHERE session_id = ? AND turn_id = ? AND role = 'user' "
        "ORDER BY seq ASC, id ASC LIMIT 1",
        (report[1], report[3]),
    )
    query_row = await asyncio.to_thread(query_result.fetchone)
    if query_row is None:
        raise HTTPException(
            status_code=404,
            detail="No user message found for the report's turn",
        )

    provenance = {
        "config_ref": report[4],
        "source_file_hashes": json.loads(report[5] or "[]"),
        "session_id": report[1],
        "message_id": report[2],
        "turn_id": report[3],
    }
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

    case_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, report_id, query, expected_outcome, provenance "
        "FROM quality_eval_cases WHERE id = ?",
        (cursor.lastrowid,),
    )
    case = await asyncio.to_thread(case_result.fetchone)
    logger.info(
        "Quality report %s converted to eval case %s", report_id, case[0]
    )
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
