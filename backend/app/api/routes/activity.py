"""Vault-scoped activity endpoint (issues #1157 / #1158, audit 2026-11-15).

One read answers "what is happening on the vaults I can read" across every
background-job family — the aggregate the Activity tray previously had to
assemble from per-family, per-vault fan-out polling (one 8s tick cost
``2..6 + 2*V + 1 + D + (0|1)`` requests; the tray's polling-budget contract
needs a single request per tick).

Family sources mirror each family's own readers (lease switches, feature
gates, and the legacy ``interrupted`` → ``failed`` reindex normalization):

- ``ingest``     — ``files`` rows in the two live statuses, with the phase
                   columns the tray renders.
- ``wiki``       — ``jobs`` queue 'wiki' when the lease is on, else
                   ``wiki_compile_jobs``; omitted entirely while the wiki
                   subsystem is disabled (a disabled family contributes zero
                   rows — it never errors the aggregate).
- ``kms``        — the same shape with queue 'kms' / ``kms_compile_jobs``
                   and the KMS master switch.
- ``reindex``    — admins only (issue #1039 kept this family behind the
                   admin gate; the server now does that scoping), including
                   NULL-vault global rows.
- ``draft-room`` — the caller's OWN drafts only (``drafts.created_by``), the
                   same owner boundary the draft-room routes enforce, for
                   every role.

Change token: a SHA-256 digest over the exact bounded per-family row
windows this response would carry (canonical JSON, response order).
``changed=false`` therefore means "the snapshot you can see is
byte-identical" — precise for inserts, status transitions, draft stage
moves, ingest phase/progress moves, title/error edits, and removals, with
bounded queries whose cost cannot grow as terminal rows accumulate.
Documented limitations: mutations entirely outside the visible window of an
old snapshot (e.g. a beyond-``limit``-deep historical row's deletion) do
not move the token, and a lease-branch flip can change row representations
(and thus the token) without a semantic change. No per-user salt:
identical visible data intentionally yields identical tokens across roles.

The route follows the repo's read-route conventions: no CSRF guard (GET
only) and no rate limit (the tray polls it; ``GET /documents/reindex/jobs``
set the unlimited-read precedent). Every SQL statement below is complete
static text — the vault set is bound as one JSON array consumed through
``json_each(?)`` — so there is no assembled SQL for bandit B608 to flag.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import get_current_active_user, get_db, get_user_accessible_vault_ids
from app.config import settings
from app.services.job_lease import ensure_jobs_schema, redact_legacy_error_text

router = APIRouter(prefix="/activity", tags=["activity"])

JobFamily = Literal["ingest", "wiki", "kms", "draft-room", "reindex"]


class ActivityJobResponse(BaseModel):
    """One job row, whatever the family. Family-specific fields are None
    outside their family; ``vault_id`` is None only for admin-visible global
    reindex rows; ``progress_percent`` is a float (SQLite REAL)."""

    family: JobFamily
    id: int
    vault_id: Optional[int] = None
    status: str
    title: str
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    draft_id: Optional[int] = None
    file_id: Optional[str] = None
    active_stage: Optional[str] = None
    phase: Optional[str] = None
    phase_message: Optional[str] = None
    progress_percent: Optional[float] = None
    processed_units: Optional[int] = None
    total_units: Optional[int] = None
    unit_label: Optional[str] = None
    error: Optional[str] = None


class ActivityJobsResponse(BaseModel):
    jobs: list[ActivityJobResponse]
    change_token: str
    changed: bool


# --- static SQL ------------------------------------------------------------
#
# Vault membership is ``IN (SELECT CAST(value AS INTEGER) FROM json_each(?))``
# with the caller's vault ids bound as one JSON array parameter; admins (all
# vaults) use the *_ALL variants that omit the membership clause entirely.

# Live-first windows (4.5 review F2): the LIVE arm (pending/running) can
# never be evicted by terminal history; the TERMINAL arm is the newest-N
# history. Live rows are bounded too (a pathological backlog must not be
# unbounded), but independently of the terminal cap.
_JOBS_WIKI_LIVE_ALL = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'wiki' AND status IN ('pending', 'running')
ORDER BY id DESC
LIMIT ?
"""
_JOBS_WIKI_LIVE_VAULT = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'wiki' AND status IN ('pending', 'running')
  AND CASE WHEN json_valid(payload_json)
       THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
       ELSE NULL END IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_JOBS_WIKI_TERMINAL_ALL = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'wiki' AND status IN ('completed', 'failed', 'cancelled')
ORDER BY id DESC
LIMIT ?
"""
_JOBS_WIKI_TERMINAL_VAULT = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'wiki' AND status IN ('completed', 'failed', 'cancelled')
  AND CASE WHEN json_valid(payload_json)
       THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
       ELSE NULL END IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_JOBS_KMS_LIVE_ALL = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'kms' AND status IN ('pending', 'running')
ORDER BY id DESC
LIMIT ?
"""
_JOBS_KMS_LIVE_VAULT = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'kms' AND status IN ('pending', 'running')
  AND CASE WHEN json_valid(payload_json)
       THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
       ELSE NULL END IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_JOBS_KMS_TERMINAL_ALL = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'kms' AND status IN ('completed', 'failed', 'cancelled')
ORDER BY id DESC
LIMIT ?
"""
_JOBS_KMS_TERMINAL_VAULT = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'kms' AND status IN ('completed', 'failed', 'cancelled')
  AND CASE WHEN json_valid(payload_json)
       THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
       ELSE NULL END IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_WIKI_LEGACY_LIVE_ALL = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM wiki_compile_jobs
WHERE status IN ('pending', 'running')
ORDER BY id DESC
LIMIT ?
"""
_WIKI_LEGACY_LIVE_VAULT = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM wiki_compile_jobs
WHERE status IN ('pending', 'running')
  AND vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_WIKI_LEGACY_TERMINAL_ALL = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM wiki_compile_jobs
WHERE status IN ('completed', 'failed', 'cancelled')
ORDER BY id DESC
LIMIT ?
"""
_WIKI_LEGACY_TERMINAL_VAULT = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM wiki_compile_jobs
WHERE status IN ('completed', 'failed', 'cancelled')
  AND vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_KMS_LEGACY_LIVE_ALL = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM kms_compile_jobs
WHERE status IN ('pending', 'running')
ORDER BY id DESC
LIMIT ?
"""
_KMS_LEGACY_LIVE_VAULT = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM kms_compile_jobs
WHERE status IN ('pending', 'running')
  AND vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_KMS_LEGACY_TERMINAL_ALL = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM kms_compile_jobs
WHERE status IN ('completed', 'failed', 'cancelled')
ORDER BY id DESC
LIMIT ?
"""
_KMS_LEGACY_TERMINAL_VAULT = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM kms_compile_jobs
WHERE status IN ('completed', 'failed', 'cancelled')
  AND vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
# NULL-vault global rows are all-vault by definition: visible to the admins
# who can trigger them, invisible to members (issue #1039 posture) — and the
# family is fetched for admins only, so these variants need no membership
# clause at all (every row, NULL-vault or not). The legacy `interrupted`
# terminal maps to `failed` in BOTH branches, exactly as the reindex list
# route does (documents.py).
_REINDEX_UNIFIED_LIVE_ADMIN = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'reindex' AND status IN ('pending', 'running')
ORDER BY id DESC
LIMIT ?
"""
_REINDEX_UNIFIED_TERMINAL_ADMIN = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       CASE WHEN status = 'interrupted' THEN 'failed' ELSE status END AS status,
       error, created_at, completed_at
FROM jobs
WHERE queue = 'reindex'
  AND status IN ('completed', 'failed', 'cancelled', 'interrupted')
ORDER BY id DESC
LIMIT ?
"""
_REINDEX_LEGACY_LIVE_ADMIN = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM document_reindex_jobs
WHERE status IN ('pending', 'running')
ORDER BY id DESC
LIMIT ?
"""
_REINDEX_LEGACY_TERMINAL_ADMIN = """
SELECT id, vault_id,
       CASE WHEN status = 'interrupted' THEN 'failed' ELSE status END AS status,
       error, created_at, completed_at
FROM document_reindex_jobs
WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')
ORDER BY id DESC
LIMIT ?
"""
_INGEST_ALL = """
SELECT id, vault_id, file_name, status, phase, phase_message,
       progress_percent, processed_units, total_units, unit_label, created_at
FROM files
WHERE status IN ('pending', 'processing')
ORDER BY created_at DESC, id DESC
LIMIT ?
"""
_INGEST_VAULT = """
SELECT id, vault_id, file_name, status, phase, phase_message,
       progress_percent, processed_units, total_units, unit_label, created_at
FROM files
WHERE status IN ('pending', 'processing')
  AND vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY created_at DESC, id DESC
LIMIT ?
"""
_DRAFT_OWN_LIVE_ALL = """
SELECT j.id, j.vault_id, j.status, j.active_stage, j.created_at, j.completed_at,
       j.draft_id, d.title AS draft_title
FROM draft_jobs j JOIN drafts d ON d.id = j.draft_id
WHERE d.created_by = ? AND j.status IN ('pending', 'running')
ORDER BY j.id DESC
LIMIT ?
"""
_DRAFT_OWN_LIVE_VAULT = """
SELECT j.id, j.vault_id, j.status, j.active_stage, j.created_at, j.completed_at,
       j.draft_id, d.title AS draft_title
FROM draft_jobs j JOIN drafts d ON d.id = j.draft_id
WHERE d.created_by = ? AND j.status IN ('pending', 'running')
  AND j.vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY j.id DESC
LIMIT ?
"""
_DRAFT_OWN_TERMINAL_ALL = """
SELECT j.id, j.vault_id, j.status, j.active_stage, j.created_at, j.completed_at,
       j.draft_id, d.title AS draft_title
FROM draft_jobs j JOIN drafts d ON d.id = j.draft_id
WHERE d.created_by = ? AND j.status IN ('completed', 'failed', 'cancelled')
ORDER BY j.id DESC
LIMIT ?
"""
_DRAFT_OWN_TERMINAL_VAULT = """
SELECT j.id, j.vault_id, j.status, j.active_stage, j.created_at, j.completed_at,
       j.draft_id, d.title AS draft_title
FROM draft_jobs j JOIN drafts d ON d.id = j.draft_id
WHERE d.created_by = ? AND j.status IN ('completed', 'failed', 'cancelled')
  AND j.vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY j.id DESC
LIMIT ?
"""


def _canonical_token(windows: dict[str, list[dict[str, Any]]]) -> str:
    payload = json.dumps(windows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@router.get("/jobs", response_model=ActivityJobsResponse)
async def list_activity_jobs(
    change_token: Optional[str] = Query(
        None, description="Token from a prior response; an equal token yields an empty delta."
    ),
    limit: int = Query(50, ge=1, le=500),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
) -> ActivityJobsResponse:
    role = user.get("role", "")
    is_admin = role in ("admin", "superadmin")
    if is_admin:
        vault_json: Optional[str] = None  # admins read all vaults
    else:
        accessible = await get_user_accessible_vault_ids(user, db)
        if not accessible:
            # No readable vaults: a well-formed empty snapshot (an empty
            # window is still a window, so the token stays meaningful).
            token = _canonical_token({})
            return ActivityJobsResponse(
                jobs=[], change_token=token, changed=change_token != token
            )
        vault_json = json.dumps([int(v) for v in accessible])

    compile_lease = bool(getattr(settings, "wiki_kms_job_lease_enabled", False))
    reindex_lease = bool(getattr(settings, "reindex_job_lease_enabled", False))
    if compile_lease or reindex_lease:
        ensure_jobs_schema(db)

    def run(sql: str, params: tuple) -> list[sqlite3.Row]:
        return db.execute(sql, params).fetchall()

    windows: dict[str, list[dict[str, Any]]] = {}
    rows: list[ActivityJobResponse] = []

    def collect(
        family: JobFamily,
        fetched: list[sqlite3.Row],
        build,
    ) -> None:
        window: list[dict[str, Any]] = []
        for r in fetched:
            row = build(r)
            rows.append(row)
            window.append(row.model_dump())
        windows[family] = window

    def fetch_live_terminal(
        live_sql: str,
        terminal_sql: str,
        live_params: tuple,
        terminal_params: tuple,
    ) -> list[sqlite3.Row]:
        # Live-first window (4.5 review F2): live rows can never be evicted
        # by newer terminal history; terminal history fills the remainder.
        live = run(live_sql, live_params)
        terminal = [
            r
            for r in run(terminal_sql, terminal_params)
            if r["id"] not in {row["id"] for row in live}
        ]
        return live + terminal

    # --- ingest (the documents surface is always on) --------------------------
    ingest = run(
        _INGEST_VAULT if vault_json else _INGEST_ALL,
        (vault_json, limit) if vault_json else (limit,),
    )
    collect(
        "ingest",
        ingest,
        lambda r: ActivityJobResponse(
            family="ingest",
            id=int(r["id"]),
            vault_id=int(r["vault_id"]),
            status=r["status"],
            title=r["file_name"] or f"Document {r['id']}",
            created_at=r["created_at"],
            file_id=str(r["id"]),
            phase=r["phase"],
            phase_message=r["phase_message"],
            progress_percent=r["progress_percent"],
            processed_units=r["processed_units"],
            total_units=r["total_units"],
            unit_label=r["unit_label"],
        ),
    )

    # --- wiki (feature gate: zero rows while disabled; never an error) --------
    if settings.wiki_enabled:
        if compile_lease:
            wiki = fetch_live_terminal(
                _JOBS_WIKI_LIVE_VAULT if vault_json else _JOBS_WIKI_LIVE_ALL,
                _JOBS_WIKI_TERMINAL_VAULT if vault_json else _JOBS_WIKI_TERMINAL_ALL,
                (vault_json, limit) if vault_json else (limit,),
                (vault_json, limit) if vault_json else (limit,),
            )
        else:
            wiki = fetch_live_terminal(
                _WIKI_LEGACY_LIVE_VAULT if vault_json else _WIKI_LEGACY_LIVE_ALL,
                _WIKI_LEGACY_TERMINAL_VAULT if vault_json else _WIKI_LEGACY_TERMINAL_ALL,
                (vault_json, limit) if vault_json else (limit,),
                (vault_json, limit) if vault_json else (limit,),
            )
        collect(
            "wiki",
            wiki,
            lambda r: ActivityJobResponse(
                family="wiki",
                id=int(r["id"]),
                vault_id=int(r["vault_id"]) if r["vault_id"] is not None else None,
                status=r["status"],
                title="Wiki compile",
                created_at=r["created_at"],
                completed_at=r["completed_at"],
                # 4.5 review F1: the family's read-time mask — pre-#951 rows
                # still carry raw str(exc) in the error column (the family
                # readers mask it at wiki_store.py/kms_store.py read time).
                error=redact_legacy_error_text(r["error"]),
            ),
        )
    else:
        windows["wiki"] = []

    # --- kms (same shape; same gate semantics) --------------------------------
    if settings.kms_enabled:
        if compile_lease:
            kms = fetch_live_terminal(
                _JOBS_KMS_LIVE_VAULT if vault_json else _JOBS_KMS_LIVE_ALL,
                _JOBS_KMS_TERMINAL_VAULT if vault_json else _JOBS_KMS_TERMINAL_ALL,
                (vault_json, limit) if vault_json else (limit,),
                (vault_json, limit) if vault_json else (limit,),
            )
        else:
            kms = fetch_live_terminal(
                _KMS_LEGACY_LIVE_VAULT if vault_json else _KMS_LEGACY_LIVE_ALL,
                _KMS_LEGACY_TERMINAL_VAULT if vault_json else _KMS_LEGACY_TERMINAL_ALL,
                (vault_json, limit) if vault_json else (limit,),
                (vault_json, limit) if vault_json else (limit,),
            )
        collect(
            "kms",
            kms,
            lambda r: ActivityJobResponse(
                family="kms",
                id=int(r["id"]),
                vault_id=int(r["vault_id"]) if r["vault_id"] is not None else None,
                status=r["status"],
                title="KMS recompile",
                created_at=r["created_at"],
                completed_at=r["completed_at"],
                # 4.5 review F1: same family read-time mask as wiki.
                error=redact_legacy_error_text(r["error"]),
            ),
        )
    else:
        windows["kms"] = []

    # --- draft-room (owner boundary for EVERY role) ---------------------------
    draft = fetch_live_terminal(
        _DRAFT_OWN_LIVE_VAULT if vault_json else _DRAFT_OWN_LIVE_ALL,
        _DRAFT_OWN_TERMINAL_VAULT if vault_json else _DRAFT_OWN_TERMINAL_ALL,
        (int(user["id"]), vault_json, limit) if vault_json else (int(user["id"]), limit),
        (int(user["id"]), vault_json, limit) if vault_json else (int(user["id"]), limit),
    )
    collect(
        "draft-room",
        draft,
        lambda r: ActivityJobResponse(
            family="draft-room",
            id=int(r["id"]),
            vault_id=int(r["vault_id"]),
            status=r["status"],
            title=r["draft_title"] or f"Draft {r['draft_id']}",
            created_at=r["created_at"],
            completed_at=r["completed_at"],
            draft_id=int(r["draft_id"]),
            active_stage=r["active_stage"],
        ),
    )

    # --- reindex (admins only, incl. NULL-vault globals) ----------------------
    if is_admin:
        reindex = fetch_live_terminal(
            _REINDEX_UNIFIED_LIVE_ADMIN if reindex_lease else _REINDEX_LEGACY_LIVE_ADMIN,
            _REINDEX_UNIFIED_TERMINAL_ADMIN
            if reindex_lease
            else _REINDEX_LEGACY_TERMINAL_ADMIN,
            (limit,),
            (limit,),
        )
        collect(
            "reindex",
            reindex,
            lambda r: ActivityJobResponse(
                family="reindex",
                id=int(r["id"]),
                vault_id=int(r["vault_id"]) if r["vault_id"] is not None else None,
                status=r["status"],
                title=(
                    f"Embedding reindex (vault {r['vault_id']})"
                    if r["vault_id"] is not None
                    else "Embedding reindex"
                ),
                created_at=r["created_at"],
                completed_at=r["completed_at"],
                error=r["error"],
            ),
        )
    else:
        windows["reindex"] = []

    token = _canonical_token(windows)
    changed = change_token != token
    return ActivityJobsResponse(
        jobs=[] if change_token is not None and not changed else rows,
        change_token=token,
        changed=changed,
    )
