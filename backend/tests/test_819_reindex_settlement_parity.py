"""Issue #819 acceptance checks: failed reindex jobs must expose the same
processed/failed/details payload on BOTH settlement paths, redacted at the
settlement boundary.

The legacy settlement (``_process_reindex_job``) persists
``json.dumps(result)`` — the ``{"processed": N, "failed": M, "details":
[...]}`` dict — into ``document_reindex_jobs.error``. The lease settlement
(``_run_reindex_job_row``) discards the dict on failure: ``JobLease`` has
no result channel for ``fail``, so a terminally failed ``jobs`` row ends
with ``result_json='{}'`` and only the composed error string — the
operator loses the per-file diagnostics on the lease transport (issue
#819 problem 2). Problem 1 (redaction at the persist boundary) was already
fixed upstream by the #702/#957 work and is pinned green here as a parity
side-assertion.

Round-2 amendment (critic F5): the stubbed failure's details now carry RAW
exception text with a filesystem sentinel
(``file_id=7: RuntimeError: /secret/acme/leak.txt``). Stubbing
``_reindex_embed_all`` bypasses the composition layer's OWN redaction, so
the persisted-payload equality plus the sentinel assertions pin the
SETTLEMENT boundary: the lease path (which today has no details
redaction loop at all) must apply the same masking the legacy boundary
applies, or the check fails.

Checks:

- AC6+AC7 (DISCRIMINATING): ONE failure driven through BOTH settlement
  paths with the same ``("failed", result, exc)`` outcome from
  ``_reindex_embed_all`` (stubbed at the documented seam); the lease row's
  persisted payload must equal the legacy row's persisted payload
  (processed/failed/details) and BOTH must be free of the raw sentinel.
  At base the lease leg's ``result_json`` stays ``'{}'`` — RED.
- AC6 contract form (DISCRIMINATING): the terminally failed lease row must
  carry a parsable payload dict with the processed/failed/details keys,
  its detail entry masked to the stable shape — RED at base for the same
  reason.
- Read-side redaction floor (DISCRIMINATING, round-2 critic F5):
  ``job_lease.redact_legacy_result_json`` must mask raw details even when
  the payload's serialized form lacks the lowercase 'error' substring (the
  current shortcut returns such payloads byte-identically, sentinel and
  all) — plus the byte-identity pin for clean payloads and the preserved
  curator-errors masking. And the reindex row reader
  (``documents._mask_reindex_row``) must sanitize a failed lease row's
  raw-sentinel ``result_json``.
- Redaction side-pin (PRESERVING, green at base): neither persisted error
  field contains the raw exception's filesystem-path sentinel (#562
  boundary; keeps the parity fix honest — the payload must arrive redacted,
  not by persisting raw text).

Harness: a temp ``run_migrations`` DB (real ``document_reindex_jobs`` +
``jobs`` via ``ensure_jobs_schema``) behind ``SQLiteConnectionPool``; the
lease leg drives the production ``_run_reindex_job_row`` (watermark stamp,
cap settle, JobLease.fail) against a real claimed row at the attempts cap;
the legacy leg drives the production ``_process_reindex_job`` (claim,
settle). Only ``_reindex_embed_all`` is doubled — the settlements under
test are production code.
"""

import json
import sqlite3

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.job_lease import ensure_jobs_schema

REINDEX_QUEUE = "reindex"
WORKER_ID = "ac819-parity-worker"

# The shared failure every leg settles: 2 files processed, 1 failed. The
# detail carries RAW exception text with a filesystem sentinel — composed
# by a composer whose own redaction the stub BYPASSES, so only the
# settlement boundary's masking stands between the sentinel and the
# persisted columns (round-2 critic F5).
SHARED_EXC = RuntimeError("provider outage reading /secret/acme/leak.txt")
SHARED_RESULT = {
    "processed": 2,
    "failed": 1,
    "details": ["file_id=7: RuntimeError: /secret/acme/leak.txt"],
    "failed_ids": [7],  # transport metadata; both settlements pop it
}
SENTINEL = "/secret/acme"


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name="ac819.db"):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    conn = _connect(db_path)
    ensure_jobs_schema(conn)
    conn.commit()
    return db_path, conn


def _install_shared_failure(processor):
    """Stub the shared embed body: every call settles the SAME partial
    failure (the documented seam both settlements consume)."""

    async def _shared_failure(*args, **kwargs):  # noqa: ARG001
        # Mutate a fresh copy per call: the settlements pop "failed_ids".
        result = dict(SHARED_RESULT)
        result["details"] = list(SHARED_RESULT["details"])
        result["failed_ids"] = list(SHARED_RESULT["failed_ids"])
        return "failed", result, SHARED_EXC

    processor._reindex_embed_all = _shared_failure  # noqa: SLF001


async def test_failed_reindex_payload_parity_lease_vs_legacy(tmp_path, monkeypatch):
    """AC6+AC7: the same partial failure through both transports persists
    equivalent payloads (processed/failed/details)."""
    db_path, conn = _seed(tmp_path, name="ac819-parity.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        # --- Legacy leg: document_reindex_jobs -------------------------
        cur = conn.execute(
            "INSERT INTO document_reindex_jobs (vault_id, status, input_json)"
            " VALUES (NULL, 'pending', '{}')"
        )
        legacy_job_id = int(cur.lastrowid)
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        _install_shared_failure(processor)
        await processor._process_reindex_job(legacy_job_id)  # noqa: SLF001

        legacy_row = conn.execute(
            "SELECT status, error FROM document_reindex_jobs WHERE id = ?",
            (legacy_job_id,),
        ).fetchone()
        assert legacy_row["status"] == "failed", (
            f"harness: the legacy leg must settle failed, got"
            f" {legacy_row['status']!r}"
        )
        legacy_payload = json.loads(legacy_row["error"])
        assert legacy_payload.get("processed") == 2, (
            "harness: the legacy payload must carry the processed count,"
            f" got {legacy_payload!r}"
        )

        # --- Lease leg: jobs (claimed row at the attempts cap) ---------
        cap = 2
        monkeypatch.setattr(settings, "jobs_max_attempts", cap)
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id,"
            " attempts) VALUES (?, ?, 'running', ?, ?)",
            (REINDEX_QUEUE, json.dumps({"vault_id": None}), WORKER_ID, cap),
        )
        conn.commit()
        lease_row = conn.execute(
            "SELECT * FROM jobs WHERE worker_id = ?", (WORKER_ID,)
        ).fetchone()
        assert lease_row is not None, "harness: the claimed lease row must exist"

        await processor._run_reindex_job_row(lease_row, WORKER_ID)  # noqa: SLF001

        jobs_row = conn.execute(
            "SELECT status, error, result_json FROM jobs WHERE worker_id = ?",
            (WORKER_ID,),
        ).fetchone()
        assert jobs_row["status"] == "failed", (
            "harness: the lease leg at the attempts cap must settle failed,"
            f" got {jobs_row['status']!r}"
        )
        lease_payload = json.loads(jobs_row["result_json"])

        assert lease_payload == legacy_payload, (
            "the SAME partial reindex failure must persist the same"
            " processed/failed/details payload on both settlement paths:"
            f" legacy={legacy_payload!r} lease={lease_payload!r} — the lease"
            " settlement drops the result dict on failure (jobs.result_json"
            " stays '{}'), so lease-mode operators lose the per-file"
            " diagnostics the legacy table still records (issue #819)"
        )
        # Round-2 (critic F5): the stub bypasses the composer's own
        # redaction, so this pins the SETTLEMENT boundary — a lease path
        # that persists the details without the legacy boundary's masking
        # fails here even if the payload keys match.
        assert SENTINEL not in json.dumps(legacy_payload), (
            "the legacy settlement must mask the raw detail text at the"
            f" persist boundary, got {legacy_payload!r}"
        )
        assert SENTINEL not in json.dumps(lease_payload), (
            "the lease settlement must apply the same detail masking as the"
            " legacy boundary when it gains its payload channel, got"
            f" {lease_payload!r}"
        )
    finally:
        pool.close_all()
        conn.close()


async def test_lease_failed_row_carries_processed_failed_details(
    tmp_path, monkeypatch
):
    """AC6 contract form: a terminally failed lease reindex row must expose
    the payload (result_json parses to a dict with the three keys)."""
    db_path, conn = _seed(tmp_path, name="ac819-contract.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        cap = 2
        monkeypatch.setattr(settings, "jobs_max_attempts", cap)
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id,"
            " attempts) VALUES (?, ?, 'running', ?, ?)",
            (REINDEX_QUEUE, json.dumps({"vault_id": None}), WORKER_ID, cap),
        )
        conn.commit()
        lease_row = conn.execute(
            "SELECT * FROM jobs WHERE worker_id = ?", (WORKER_ID,)
        ).fetchone()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        _install_shared_failure(processor)
        await processor._run_reindex_job_row(lease_row, WORKER_ID)  # noqa: SLF001

        jobs_row = conn.execute(
            "SELECT status, result_json FROM jobs WHERE worker_id = ?",
            (WORKER_ID,),
        ).fetchone()
        assert jobs_row["status"] == "failed", (
            f"harness: the cap settle must fail the row, got"
            f" {jobs_row['status']!r}"
        )
        payload = json.loads(jobs_row["result_json"])
        assert isinstance(payload, dict) and set(
            ["processed", "failed", "details"]
        ) <= set(payload), (
            "a terminally failed lease reindex job must carry its"
            " processed/failed/details payload (jobs.result_json), got"
            f" {jobs_row['result_json']!r} — JobLease.fail has no result"
            " channel, so the payload is dropped (issue #819)"
        )
        assert payload["processed"] == 2
        assert payload["failed"] == 1
        # The detail must survive in the MASKED stable shape (the settlement
        # boundary's masking — the raw input carried the sentinel), never as
        # the raw composer text.
        assert len(payload["details"]) == 1 and payload["details"][
            0
        ].startswith("file_id=7: "), (
            "the persisted detail must keep the file_id=7: shape, got"
            f" {payload['details']!r}"
        )
        assert SENTINEL not in json.dumps(payload), (
            "the lease payload channel must not leak the raw filesystem"
            f" sentinel, got {jobs_row['result_json']!r}"
        )
    finally:
        pool.close_all()
        conn.close()


async def test_both_settlements_redact_raw_exception_text(tmp_path, monkeypatch):
    """PRESERVING (green at base): neither persisted error field echoes the
    raw exception's filesystem path — the #562 boundary the parity fix must
    not regress while adding the payload channel."""
    db_path, conn = _seed(tmp_path, name="ac819-redaction.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        cur = conn.execute(
            "INSERT INTO document_reindex_jobs (vault_id, status, input_json)"
            " VALUES (NULL, 'pending', '{}')"
        )
        legacy_job_id = int(cur.lastrowid)
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        _install_shared_failure(processor)
        await processor._process_reindex_job(legacy_job_id)  # noqa: SLF001
        legacy_error = conn.execute(
            "SELECT error FROM document_reindex_jobs WHERE id = ?",
            (legacy_job_id,),
        ).fetchone()["error"]

        cap = 2
        monkeypatch.setattr(settings, "jobs_max_attempts", cap)
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id,"
            " attempts) VALUES (?, ?, 'running', ?, ?)",
            (REINDEX_QUEUE, json.dumps({"vault_id": None}), WORKER_ID, cap),
        )
        conn.commit()
        lease_row = conn.execute(
            "SELECT * FROM jobs WHERE worker_id = ?", (WORKER_ID,)
        ).fetchone()
        await processor._run_reindex_job_row(lease_row, WORKER_ID)  # noqa: SLF001
        lease_row_after = conn.execute(
            "SELECT error, result_json FROM jobs WHERE worker_id = ?",
            (WORKER_ID,),
        ).fetchone()

        assert SENTINEL not in (legacy_error or ""), (
            "the legacy settlement must keep redacting the raw exception"
            f" (found the path sentinel in {legacy_error!r})"
        )
        assert SENTINEL not in (lease_row_after["error"] or ""), (
            "the lease settlement must keep redacting the raw exception"
            f" (found the path sentinel in {lease_row_after['error']!r})"
        )
        assert SENTINEL not in (lease_row_after["result_json"] or ""), (
            "the lease payload channel must carry redacted stable-code"
            " details only"
        )
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# R3 (round-2 amendment, critic F5) — read-side redaction floor
# ---------------------------------------------------------------------------


def test_redact_legacy_result_json_masks_details_without_error_substring():
    """C13: the result_json masker must not let its 'error'-substring
    shortcut return a raw-sentinel payload byte-identically — while clean
    payloads (reindex-shaped AND free-form operational) come back
    byte-identically and recognized curator errors stay masked.

    ``redact_legacy_result_json`` short-circuits with ``"error" not in
    result_json`` — a failed-reindex payload whose serialized form carries
    no lowercase 'error' (the #819 shape: processed/failed/details with a
    ``RuntimeError:`` detail) skips the masking entirely, so the raw
    filesystem sentinel would surface to readers. The planned fix scopes
    detail masking to reindex-shaped entries (^file_id=\\d+: ...) — the
    byte-identity pins below pin that scoping: wiki-shaped and KMS-shaped
    operational details strings are NEVER rewritten (round-3, critics
    R2-5/R2-6; the curator fixture uses the transport_error code the
    existing masker already recognizes — no vocabulary expansion). The
    green pins run FIRST so base evidence exists for each; the
    discriminating shortcut-hole assertion runs last."""
    from app.services.job_lease import redact_legacy_result_json

    # --- GREEN pins (base and post-fix) --------------------------------
    reindex_clean_payload = json.dumps(
        {
            "processed": 2,
            "failed": 1,
            "details": [
                "file_id=7: EMBEDDING_FAILED: the embedding provider"
                " failed; the document itself parsed correctly"
            ],
        }
    )
    assert (
        redact_legacy_result_json(reindex_clean_payload)
        == reindex_clean_payload
    ), (
        "a clean reindex payload (stable-code detail) must come back"
        " byte-identically"
    )

    wiki_shaped_payload = json.dumps(
        {"details": ["Claim id=1 has no provenance sources."]}
    )
    assert (
        redact_legacy_result_json(wiki_shaped_payload) == wiki_shaped_payload
    ), (
        "a wiki-shaped operational details payload is free-form text, not a"
        " reindex-shaped entry — it must come back byte-identically under"
        " the planned shape-gated masking (never rewritten)"
    )

    kms_shaped_payload = json.dumps(
        {
            "details": [
                "compile skipped: vault has no active KMS compile to"
                " invalidate"
            ]
        }
    )
    assert (
        redact_legacy_result_json(kms_shaped_payload) == kms_shaped_payload
    ), (
        "a KMS-shaped operational details payload is free-form text, not a"
        " reindex-shaped entry — it must come back byte-identically under"
        " the planned shape-gated masking (never rewritten)"
    )

    curator_payload = json.dumps(
        {
            "curator": {
                "errors": [
                    "transport_error: RuntimeError: /secret/acme/curator.txt"
                ]
            }
        }
    )
    curator_masked = redact_legacy_result_json(curator_payload)
    assert SENTINEL not in (curator_masked or ""), (
        "the existing curator-errors masking must be preserved for the"
        f" recognized transport_error code, got {curator_masked!r}"
    )

    # --- DISCRIMINATING (RED at base) ----------------------------------
    raw_details_payload = json.dumps(
        {
            "processed": 2,
            "failed": 1,
            "details": ["file_id=7: RuntimeError: /secret/acme/leak.txt"],
        }
    )
    assert "error" not in raw_details_payload, (
        "harness: the discriminating payload's serialized form must not"
        " contain the lowercase 'error' substring (the shortcut's gate)"
    )
    masked = redact_legacy_result_json(raw_details_payload)
    assert SENTINEL not in (masked or ""), (
        "a details entry carrying raw exception text must be masked even"
        " when the payload's serialized form lacks the 'error' substring —"
        f" the shortcut returned it raw: {masked!r} (issue #819 round-2"
        " critic F5)"
    )


def test_redact_legacy_result_json_masks_shapeless_reindex_details():
    """R3-1 PRESERVING pin (round-4): a SHAPE-LESS raw reindex detail — a
    payload with the integer processed/failed pair but a details entry that
    carries NO ``file_id=<id>: `` prefix — is already masked at base (the
    existing entry masker maps it to the REDACTED_LEGACY_ERROR marker), and
    the planned payload-level gate keeps it masked (a payload carrying BOTH
    integer 'processed' and 'failed' keys is a reindex result: ALL its
    details entries are masked, shaped and shape-less). Green at base and
    must stay green."""
    from app.services.job_lease import redact_legacy_result_json

    shapeless_payload = json.dumps(
        {
            "processed": 0,
            "failed": 1,
            "details": [
                "RuntimeError: disk I/O error on /secret/acme/leak.txt"
            ],
        }
    )
    masked = redact_legacy_result_json(shapeless_payload)
    assert SENTINEL not in (masked or ""), (
        "a shape-less raw reindex detail in a payload carrying the"
        " processed/failed pair must be masked — the base masker already"
        " maps it to the REDACTED_LEGACY_ERROR marker and the planned"
        f" payload-level gate must keep it masked, got {masked!r}"
        " (issue #819 round-4 R3-1)"
    )
    assert isinstance(masked, str) and masked.strip(), (
        "harness: the masked payload must stay a non-empty string"
    )


def test_reindex_row_mask_sanitizes_failed_result_json():
    """C14: the reindex job reader's row mask must sanitize a failed lease
    row's raw-sentinel result_json (the same shortcut currently lets the
    sentinel through to the response-visible field)."""
    from app.api.routes.documents import _mask_reindex_row  # noqa: SLF001

    failed_row = {
        "id": 9,
        "status": "failed",
        "error": "attempt_cap_exceeded: EMBEDDING_FAILED: provider failed",
        "result_json": json.dumps(
            {
                "processed": 2,
                "failed": 1,
                "details": ["file_id=7: RuntimeError: /secret/acme/leak.txt"],
            }
        ),
    }
    masked = _mask_reindex_row(dict(failed_row))
    visible = json.dumps(masked)
    assert SENTINEL not in visible, (
        "the response-visible projection of a failed reindex row must not"
        f" carry the raw filesystem sentinel, got {visible!r} (issue #819"
        " round-2 critic F5)"
    )
    assert isinstance(masked.get("result_json"), str) and masked[
        "result_json"
    ].strip(), (
        "harness: result_json must stay a non-empty string after masking"
    )
