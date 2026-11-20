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
from app.services.job_lease import ensure_jobs_schema

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

_JOBS_WIKI_ALL = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'wiki'
ORDER BY id DESC
LIMIT ?
"""
_JOBS_WIKI_VAULT = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'wiki'
  AND CASE WHEN json_valid(payload_json)
       THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
       ELSE NULL END IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_JOBS_KMS_ALL = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'kms'
ORDER BY id DESC
LIMIT ?
"""
_JOBS_KMS_VAULT = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       status, error, created_at, completed_at
FROM jobs
WHERE queue = 'kms'
  AND CASE WHEN json_valid(payload_json)
       THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
       ELSE NULL END IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_WIKI_LEGACY_ALL = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM wiki_compile_jobs
ORDER BY id DESC
LIMIT ?
"""
_WIKI_LEGACY_VAULT = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM wiki_compile_jobs
WHERE vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
_KMS_LEGACY_ALL = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM kms_compile_jobs
ORDER BY id DESC
LIMIT ?
"""
_KMS_LEGACY_VAULT = """
SELECT id, vault_id, status, error, created_at, completed_at
FROM kms_compile_jobs
WHERE vault_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
ORDER BY id DESC
LIMIT ?
"""
# NULL-vault global rows are all-vault by definition: visible to the admins
# who can trigger them, invisible to members (issue #1039 posture) — and the
# family is fetched for admins only, so these variants need no membership
# clause at all (every row, NULL-vault or not). The legacy `interrupted`
# terminal maps to `failed` in BOTH branches, exactly as the reindex list
# route does (documents.py).
_REINDEX_UNIFIED_ADMIN = """
SELECT id,
       CASE WHEN json_valid(payload_json)
            THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
            ELSE NULL END AS vault_id,
       CASE WHEN status = 'interrupted' THEN 'failed' ELSE status END AS status,
       error, created_at, completed_at
FROM jobs
WHERE queue = 'reindex'
ORDER BY id DESC
LIMIT ?
"""
_REINDEX_LEGACY_ADMIN = """
SELECT id, vault_id,
       CASE WHEN status = 'interrupted' THEN 'failed' ELSE status END AS status,
       error, created_at, completed_at
FROM document_reindex_jobs
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
_DRAFT_OWN_ALL = """
SELECT j.id, j.vault_id, j.status, j.active_stage, j.created_at, j.completed_at,
       j.draft_id, d.title AS draft_title
FROM draft_jobs j JOIN drafts d ON d.id = j.draft_id
WHERE d.created_by = ?
ORDER BY j.id DESC
LIMIT ?
"""
_DRAFT_OWN_VAULT = """
SELECT j.id, j.vault_id, j.status, j.active_stage, j.created_at, j.completed_at,
       j.draft_id, d.title AS draft_title
FROM draft_jobs j JOIN drafts d ON d.id = j.draft_id
WHERE d.created_by = ?
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
            wiki = run(
                _JOBS_WIKI_VAULT if vault_json else _JOBS_WIKI_ALL,
                (vault_json, limit) if vault_json else (limit,),
            )
        else:
            wiki = run(
                _WIKI_LEGACY_VAULT if vault_json else _WIKI_LEGACY_ALL,
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
                error=r["error"],
            ),
        )
    else:
        windows["wiki"] = []

    # --- kms (same shape; same gate semantics) --------------------------------
    if settings.kms_enabled:
        if compile_lease:
            kms = run(
                _JOBS_KMS_VAULT if vault_json else _JOBS_KMS_ALL,
                (vault_json, limit) if vault_json else (limit,),
            )
        else:
            kms = run(
                _KMS_LEGACY_VAULT if vault_json else _KMS_LEGACY_ALL,
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
                error=r["error"],
            ),
        )
    else:
        windows["kms"] = []

    # --- draft-room (owner boundary for EVERY role) ---------------------------
    draft = run(
        _DRAFT_OWN_VAULT if vault_json else _DRAFT_OWN_ALL,
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
        reindex = run(
            _REINDEX_UNIFIED_ADMIN if reindex_lease else _REINDEX_LEGACY_ADMIN,
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
