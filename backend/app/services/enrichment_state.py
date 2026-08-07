"""Atom-scoped enrichment stage state + fingerprints for multimodal RAG (issue #461).

This module drives the ``enrich`` stage on the #460 ``ingestion_stage_states`` table
(scoped to an individual ``document_atoms`` row) plus the derived ``document_atom_enrichments``
records. It deliberately reuses the #460 stage-state foundation rather than adding a parallel
state model, so startup recovery, the unique partial-index idempotency, and cascade semantics
all hold for enrichment too.

The stage ``status`` vocabulary is enforced by the SQLite CHECK constraint on
``ingestion_stage_states`` and mirrored here as Python constants.

Design invariants (issue #461):
- The atom-scoped stage row FK is the ``document_atoms`` *rowid* PK (schema uses
  ``atom_id INTEGER REFERENCES document_atoms(id)``), not the opaque ``atom_id`` string.
  The opaque id is stored/looked up separately.
- The input fingerprint captures everything that determines the derived output:
  source generation + asset SHA, relevant neighbor/caption/footnote hashes,
  atom schema + enrichment implementation version, prompt version/hash,
  model/logical mode + schema version + effective caps. **base_host is intentionally
  excluded** (a host cutover must not invalidate every fingerprint and re-flood calls).
- A completion is rejected when its input fingerprint is stale: an old job must never
  overwrite a newer generation's derived record.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

# The enrichment stage name on ingestion_stage_states.
ENRICH_STAGE = "enrich"

# Stage statuses (mirror the ingestion_stage_states CHECK constraint).
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
PARTIAL = "partial"
FAILED_RETRYABLE = "failed_retryable"
FAILED_PERMANENT = "failed_permanent"
SKIPPED_POLICY = "skipped_policy"
SKIPPED_NOT_APPLICABLE = "skipped_not_applicable"

ALL_STATUSES = frozenset(
    {
        PENDING,
        RUNNING,
        SUCCEEDED,
        PARTIAL,
        FAILED_RETRYABLE,
        FAILED_PERMANENT,
        SKIPPED_POLICY,
        SKIPPED_NOT_APPLICABLE,
    }
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(payload: Any) -> str:
    """Canonical JSON composition so equal inputs always hash equal regardless of
    dict ordering (matches the #460 generation fingerprint convention)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_input_fingerprint(
    *,
    generation_hash: str,
    asset_sha: Optional[str],
    neighbor_hashes: tuple[str, ...] = (),
    atom_schema_version: int,
    impl_version: str,
    prompt_version: str,
    model: str,
    logical_mode: str,
    response_schema_version: str,
    max_pixels: int,
    max_asset_bytes: int,
) -> str:
    """Deterministic fingerprint of the inputs/caps that determine a derived record.

    ``base_host`` is intentionally NOT an input: redeploying the same model at a new
    host must invalidate only the (blocked) connection, never force re-flood
    enrichment. Everything that can change the meaning of the proxy is included, so
    the completion fingerprint rejects stale jobs when any of these change mid-flight.
    """
    payload = _canonical(
        {
            "generation_hash": generation_hash,
            "asset_sha": asset_sha,
            "neighbor_hashes": list(neighbor_hashes),
            "atom_schema_version": atom_schema_version,
            "impl_version": impl_version,
            "prompt_version": prompt_version,
            "model": model,
            "logical_mode": logical_mode,
            "response_schema_version": response_schema_version,
            "max_pixels": max_pixels,
            "max_asset_bytes": max_asset_bytes,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_atom_pk(conn, *, file_id: int, generation_hash: str, atom_id: str) -> Optional[int]:
    """Return the document_atoms rowid PK for an opaque atom_id (or None)."""
    row = conn.execute(
        "SELECT id FROM document_atoms "
        "WHERE file_id = ? AND generation_hash = ? AND atom_id = ?",
        (file_id, generation_hash, atom_id),
    ).fetchone()
    return row["id"] if row else None


def load_atom(conn, *, atom_pk: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT atom_id, file_id, generation_hash, ordinal, kind, raw_text, "
        "page_number, bbox_json, section_path_json, caption, parent_atom_id, "
        "asset_id, parser_fingerprint "
        "FROM document_atoms WHERE id = ?",
        (atom_pk,),
    ).fetchone()
    return dict(row) if row else None


def _atom_stage_status(conn, *, file_id: int, generation_hash: str, atom_pk: int, stage: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT status, input_fingerprint, attempts, error_code "
        "FROM ingestion_stage_states "
        "WHERE file_id = ? AND generation_hash = ? AND atom_id = ? AND stage = ?",
        (file_id, generation_hash, atom_pk, stage),
    ).fetchone()
    return dict(row) if row else None


def claim_atom_stage(
    conn,
    *,
    file_id: int,
    generation_hash: str,
    atom_pk: int,
    stage: str,
    input_fingerprint: str,
    implementation_version: str,
    model_id: Optional[str],
    prompt_id: Optional[str],
    config_id: Optional[str],
) -> None:
    """Atomically claim an atom's enrichment stage as running.

    Uses ``INSERT OR REPLACE`` (the partial unique index makes this idempotent per
    atom+stage). No DB connection is held by the caller across the provider call.
    """
    conn.execute(
        "INSERT OR REPLACE INTO ingestion_stage_states "
        "(file_id, atom_id, generation_hash, stage, status, input_fingerprint, "
        " implementation_version, model_id, prompt_id, config_id, attempts, "
        " error_code, error_message, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, NULL)",
        (
            file_id,
            atom_pk,
            generation_hash,
            stage,
            RUNNING,
            input_fingerprint,
            implementation_version,
            model_id,
            prompt_id,
            config_id,
            _iso_now(),
        ),
    )


def complete_atom_stage(
    conn,
    *,
    file_id: int,
    generation_hash: str,
    atom_pk: int,
    stage: str,
    input_fingerprint: str,
    status: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    attempts: int = 1,
) -> bool:
    """Record a completion/state, REJECTING a stale fingerprint.

    If the current stage row's input_fingerprint differs from ``input_fingerprint``
    (i.e. the source generation moved on), the completion is a no-op and returns
    False so an old job cannot overwrite a newer generation. Otherwise the row is
    updated in place (preserving any prior attempts) and returns True.
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"invalid enrichment stage status: {status}")
    current = _atom_stage_status(
        conn, file_id=file_id, generation_hash=generation_hash, atom_pk=atom_pk, stage=stage
    )
    existing_fp = current["input_fingerprint"] if current else None
    if existing_fp is not None and existing_fp != input_fingerprint:
        return False

    now = _iso_now()
    completed = now if status not in (PENDING, RUNNING, FAILED_RETRYABLE) else None
    cursor = conn.execute(
        "UPDATE ingestion_stage_states SET status = ?, error_code = ?, "
        "error_message = ?, attempts = ?, updated_at = ?, completed_at = COALESCE(?, completed_at) "
        "WHERE file_id = ? AND atom_id = ? AND generation_hash = ? AND stage = ? "
        "AND input_fingerprint = ?",
        (
            status,
            error_code,
            error_message,
            attempts,
            now,
            completed,
            file_id,
            atom_pk,
            generation_hash,
            stage,
            input_fingerprint,
        ),
    )
    # Use the UPDATE's own rowcount, not the connection-wide total_changes count
    # (which is cumulative for a pooled, reused connection's lifetime and would
    # stay >0 forever after the first write, misreporting a 0-row guarded UPDATE
    # as a success).
    return cursor.rowcount > 0


def list_enrichable_atoms(conn, *, file_id: int, generation_hash: str, stage: str) -> list[dict]:
    """Return atom rows for kinds requiring enrichment whose stage is actionable.

    Actionable statuses: pending, failed_retryable, and (re-runnable) skipped_*.
    ``succeeded``/``partial``/``failed_permanent``/``running`` are not returned.
    """
    rows = conn.execute(
        "SELECT atom_id, id AS atom_pk, kind, asset_id, raw_text, caption, "
        "section_path_json, page_number "
        "FROM document_atoms "
        "WHERE file_id = ? AND generation_hash = ? "
        "AND kind IN ('image','chart','table','equation')"
        "ORDER BY ordinal",
        (file_id, generation_hash),
    ).fetchall()
    out = []
    for row in rows:
        existing = _atom_stage_status(
            conn, file_id=file_id, generation_hash=generation_hash,
            atom_pk=row["atom_pk"], stage=stage,
        )
        status = existing["status"] if existing else PENDING
        if status in (PENDING, FAILED_RETRYABLE, SKIPPED_NOT_APPLICABLE):
            out.append(dict(row))
    return out


def recover_stranded_atom_stages(conn) -> int:
    """Reclaim stranded atom 'enrich' work (running) back to pending at startup.

    Returns the number of rows reclaimed. File-level chunk-enrichment recovery
    (files.enrichment_status) is intentionally separate and untouched.
    """
    cur = conn.execute(
        "UPDATE ingestion_stage_states SET status = ?, updated_at = ? "
        "WHERE stage = ? AND status = ? AND atom_id IS NOT NULL",
        (PENDING, _iso_now(), ENRICH_STAGE, RUNNING),
    )
    return cur.rowcount


def aggregate_stage_status(conn, *, file_id: int, stage: str) -> dict[str, int]:
    """Aggregate atom-scoped stage status counts for one file (S7 status UI)."""
    counts = {s: 0 for s in ALL_STATUSES}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM ingestion_stage_states "
        "WHERE file_id = ? AND stage = ? AND atom_id IS NOT NULL GROUP BY status",
        (file_id, stage),
    ).fetchall()
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


# ---------------------------------------------------------------------------
# Derived record persistence (issue #461 S6): description/search aids + provenance
# ---------------------------------------------------------------------------


def upsert_derived(
    conn,
    *,
    file_id: int,
    generation_hash: str,
    atom_id: str,
    input_fingerprint: str,
    description: str,
    retrieval_aids: list[str],
    prompt_version: str,
    schema_version: str,
    impl_version: str,
    provider_snapshot: dict[str, str],
) -> None:
    """Create/replace the derived record for one atom+generation (no commit).

    Raw atoms are never touched. The unique key (file_id, generation_hash,
    atom_id) gives one coherent derived record per atom+generation.
    """
    now = _iso_now()
    conn.execute(
        "INSERT OR REPLACE INTO document_atom_enrichments "
        "(atom_id, file_id, generation_hash, input_fingerprint, description, "
        " retrieval_aids_json, prompt_version, schema_version, impl_version, "
        " provider_snapshot_json, updated_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            atom_id,
            file_id,
            generation_hash,
            input_fingerprint,
            description,
            _canonical(retrieval_aids),
            prompt_version,
            schema_version,
            impl_version,
            _canonical(provider_snapshot),
            now,
            now,
        ),
    )


def load_derived(conn, *, file_id: int, generation_hash: str, atom_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT input_fingerprint, description, retrieval_aids_json, prompt_version, "
        "schema_version, impl_version, provider_snapshot_json "
        "FROM document_atom_enrichments "
        "WHERE file_id = ? AND generation_hash = ? AND atom_id = ?",
        (file_id, generation_hash, atom_id),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        data["retrieval_aids"] = json.loads(data.pop("retrieval_aids_json") or "[]")
    except (TypeError, ValueError):
        data["retrieval_aids"] = []
    try:
        data["provider_snapshot"] = json.loads(data.pop("provider_snapshot_json") or "{}")
    except (TypeError, ValueError):
        data["provider_snapshot"] = {}
    return data


def set_proxy_vector_id(conn, *, file_id: int, generation_hash: str, atom_id: str, input_fingerprint: str, proxy_vector_id: str) -> int:
    """Record the LanceDB row id of the derived proxy for an atom+generation.

    Only persists when the atom's current derived-record fingerprint matches
    ``input_fingerprint`` (the fingerprint the proxy was built from), so a stale
    or concurrently-replaced enrichment never has a vector id pinned to the wrong
    derived row. Returns the number of rows updated (0 when stale/unmatched).
    """
    cursor = conn.execute(
        "UPDATE document_atom_enrichments SET proxy_vector_id = ?, updated_at = ? "
        "WHERE file_id = ? AND generation_hash = ? AND atom_id = ? "
        "AND input_fingerprint = ?",
        (proxy_vector_id, _iso_now(), file_id, generation_hash, atom_id, input_fingerprint),
    )
    return cursor.rowcount


def prior_proxy_ids(conn, *, file_id: int, generation_hash: str) -> list[str]:
    """Return previously recorded proxy vector ids for a file/generation."""
    rows = conn.execute(
        "SELECT proxy_vector_id FROM document_atom_enrichments "
        "WHERE file_id = ? AND generation_hash = ? AND proxy_vector_id IS NOT NULL",
        (file_id, generation_hash),
    ).fetchall()
    return [r["proxy_vector_id"] for r in rows]


def aggregate_derived_counts(conn, *, file_id: int) -> dict[str, int]:
    """Aggregate derived-record status counts for one file (values are not NULL
    only for enriched/attempted atoms)."""
    counts: dict[str, int] = {
        "enriched": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
    }
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM document_atom_enrichments "
        "WHERE file_id = ? GROUP BY status",
        (file_id,),
    ).fetchall()
    for row in rows:
        s = row["status"]
        if s == SUCCEEDED:
            counts["enriched"] += row["n"]
        elif s == PARTIAL:
            counts["partial"] += row["n"]
        elif s in (FAILED_RETRYABLE, FAILED_PERMANENT):
            counts["failed"] += row["n"]
        elif s in (SKIPPED_POLICY, SKIPPED_NOT_APPLICABLE):
            counts["skipped"] += row["n"]
    return counts
