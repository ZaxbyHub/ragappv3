"""Follow-up #1073 status-clobber EXTENSIONS, wave 3 (critic Round 2).

Implementation-shape-targeted specs for the amended fix design: the
EXISTING ``DocumentProcessor.set_enrichment_status`` computes the written
status INSIDE the SQL as a CASE over this file's ENRICH_STAGE atom rows
with exactly the atom sync's mapping (zero rows -> 'complete'; all
succeeded -> 'complete'; all failed_permanent -> 'error'; anything else ->
'partial', error NULL), and the cancellation CAS is
``_mark_enrichment_stale_if_current_job(file_id, processing_started_at,
error_message=CANCEL)`` keyed on ``enrichment_status='processing'`` AND
``enrichment_updated_at=<this attempt's stamp>``. The frozen harness (real
sqlite DB, REAL production functions on a stub the frozen module builds)
is imported from ``test_followup_1073_status_clobber`` — none of these
specs needs a stub binding the frozen harness does not already provide.

* C.1 DISCRIMINATING — the early-complete arm over all-FAILED_PERMANENT
  stages yields 'error'/NULL. RED at base, GREEN after the fix.
* C.2 DISCRIMINATING — the no-candidates early-complete arm
  (document_processor.py ~2572-2575) is atom-aware too. RED at base,
  GREEN after the fix.
* C.3 DISCRIMINATING — the early-complete arm over in-flight (RUNNING)
  stages yields 'partial' (in-flight counts as 'anything else' in the
  shared mapping), and the REAL atom sync later settles the completed
  stages to 'complete'. RED at base (first half), GREEN after the fix.
* C.4 DISCRIMINATING — a newer attempt's 'processing' re-stamp is not
  clobbered by the older attempt's cancellation write. RED at base,
  GREEN after the fix.
* C.5 PRESERVING — the writer-8 hash-rotation stale marker: the atomic
  hash-CAS must stay behavior-identical to today's check-then-write.
  GREEN at base and head.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_followup_1073_status_clobber import (  # noqa: I001
    _cancel_and_drain,
    _enrichment_row,
    _make_db,
    _make_job_stub,
    _run_atom_sync,
    _seed_atom_stages,
    _start_real_job,
    _wait_for_status,
)

from app.models.database import SQLiteConnectionPool
from app.services import enrichment_state as est

_STALE_MARKER_TEXT = "became stale"


def _open_pool(db_path: str) -> SQLiteConnectionPool:
    """Open a pool on the seeded DB (mirror of the frozen file's pattern)."""
    return SQLiteConnectionPool(db_path)


def _full_row(pool: SQLiteConnectionPool, file_id: int) -> sqlite3.Row:
    """Read status + error + the stamp (needed by the re-stamp spec)."""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT enrichment_status, enrichment_error, enrichment_updated_at "
            "FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()


def _claim_atom_stages_running(
    pool: SQLiteConnectionPool, file_id: int, ordinals: tuple[int, ...]
) -> dict[int, str]:
    """Seed ENRICH_STAGE rows left RUNNING (claim only, no completion).

    Mirrors the frozen ``_seed_atom_stages`` seeding (same table insert,
    same claim call) but stops before ``complete_atom_stage``; returns the
    per-ordinal fingerprints so a later completion can reuse them.
    """
    generation_hash = "gen1"
    fingerprints: dict[int, str] = {}
    with pool.connection() as conn:
        for ordinal in ordinals:
            atom_pk = conn.execute(
                "INSERT INTO document_atoms (atom_id, schema_version, file_id, "
                "generation_hash, ordinal, kind, raw_text) "
                "VALUES (?, 1, ?, ?, ?, 'image', 'ocr') RETURNING id",
                (f"atom-{ordinal}", file_id, generation_hash, ordinal),
            ).fetchone()["id"]
            fingerprint = f"fp-{ordinal}"
            fingerprints[ordinal] = fingerprint
            claimed = est.claim_atom_stage(
                conn,
                file_id=file_id,
                generation_hash=generation_hash,
                atom_pk=atom_pk,
                stage=est.ENRICH_STAGE,
                input_fingerprint=fingerprint,
                implementation_version="1",
                model_id="m",
                prompt_id="v1",
                config_id="v1",
            )
            assert claimed, f"claiming atom stage ordinal={ordinal} failed"
        conn.commit()
    return fingerprints


# ---------------------------------------------------------------------------
# C.1: early-complete over all-FAILED_PERMANENT -> 'error'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_complete_yields_error_when_all_atom_stages_failed_permanently(
    tmp_path,
):
    """The early-complete arm maps an all-failed atom batch to 'error'/NULL.

    Row 'processing'/NULL over two FAILED_PERMANENT ENRICH_STAGE rows; the
    REAL ``run_enrichment_job`` takes the no-service early-complete arm.
    The written status must be computed from the stages — the atom sync's
    all-``FAILED_PERMANENT`` vocabulary 'error' with NULL error — never a
    blind 'complete'. RED at base (the arm writes 'complete'), GREEN after
    the fix.

    Post-fix mutation that turns this RED: replacing the CASE's
    all-failed_permanent -> 'error' branch with 'complete'.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="processing", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        counts = _seed_atom_stages(
            pool, file_id, [(0, est.FAILED_PERMANENT), (1, est.FAILED_PERMANENT)]
        )
        assert counts[est.FAILED_PERMANENT] == 2, (
            f"precondition: expected 2 FAILED_PERMANENT stages, "
            f"counts={counts!r}"
        )

        stub, chunk = _make_job_stub(pool)
        stub._get_chunk_enrichment_service = lambda vault_id, file_id: None
        await _start_real_job(stub, file_id, vault_id, chunk)

        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "error",
            None,
        ), (
            "chunk complete masked an all-failed atom aggregate: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} after the REAL "
            "run_enrichment_job early-complete arm ran with no enrichment "
            "service configured, but both ENRICH_STAGE atom rows are "
            "FAILED_PERMANENT and the shared mapping must yield 'error' "
            "with a NULL error, never 'complete'"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# C.2: the no-candidates early-complete arm is atom-aware too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_candidates_complete_arm_is_atom_aware(tmp_path):
    """The no-candidates early-complete arm also reads the atom stages.

    The enrichment service IS configured (the parking double — never
    awaited), but ``_candidate_chunks_for_enrichment`` returns no
    candidates, so the REAL job takes the second early-complete arm
    (document_processor.py ~2572-2575). Row 'processing'/NULL over a mixed
    [SUCCEEDED, FAILED_PERMANENT] stage set -> 'partial'. RED at base (the
    arm writes 'complete'), GREEN after the fix.

    Post-fix mutation that turns this RED: giving the no-candidates arm a
    status write that bypasses the stage-aware CASE.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="processing", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        counts = _seed_atom_stages(
            pool, file_id, [(0, est.SUCCEEDED), (1, est.FAILED_PERMANENT)]
        )
        assert counts[est.FAILED_PERMANENT] == 1, (
            f"precondition: expected 1 FAILED_PERMANENT stage, "
            f"counts={counts!r}"
        )

        stub, chunk = _make_job_stub(pool)
        # A configured service but zero candidates -> the :2572-2575 arm.
        stub._candidate_chunks_for_enrichment = lambda file_id, chunks: []
        await _start_real_job(stub, file_id, vault_id, chunk)

        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "partial",
            None,
        ), (
            "no-candidates complete arm ignored atom stages: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} after the REAL "
            "run_enrichment_job no-candidates arm ran, but the file's atom "
            "stages are 1 SUCCEEDED + 1 FAILED_PERMANENT and the shared "
            "mapping must yield 'partial', never a blind 'complete'"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# C.3: early-complete over in-flight stages -> 'partial', then sync settles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_complete_over_in_flight_atoms_then_sync_settles(tmp_path):
    """In-flight (RUNNING) atom stages keep the early-complete at 'partial'.

    Two halves:

    1. Two ENRICH_STAGE rows are claimed but never completed (RUNNING);
       row 'processing'/NULL; the REAL early-complete arm runs. In-flight
       counts as 'anything else' in the shared mapping, so the row must
       read 'partial'/NULL — reporting 'complete' while atom enrichment is
       still in flight is the same false-complete class. RED at base (the
       arm writes 'complete').
    2. Both stages then complete SUCCEEDED (same fingerprints) and the
       REAL atom sync settles the row to 'complete'/NULL — the 'partial'
       is a live state the atom pipeline advances, not a dead end.

    Post-fix mutation that turns this RED (half 1): collapsing the CASE's
    'anything else -> partial' branch to 'complete' for RUNNING-only
    stage sets.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="processing", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        fingerprints = _claim_atom_stages_running(pool, file_id, (0, 1))
        with pool.connection() as conn:
            counts = est.aggregate_stage_status(
                conn, file_id=file_id, stage=est.ENRICH_STAGE
            )
        assert counts[est.RUNNING] == 2, (
            f"precondition: expected 2 RUNNING stages, counts={counts!r}"
        )

        stub, chunk = _make_job_stub(pool)
        stub._get_chunk_enrichment_service = lambda vault_id, file_id: None
        await _start_real_job(stub, file_id, vault_id, chunk)

        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "partial",
            None,
        ), (
            "chunk complete reported complete over in-flight atom stages: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} after the REAL "
            "run_enrichment_job early-complete arm ran, but both ENRICH_"
            "STAGE atom rows are still RUNNING and in-flight counts as "
            "'anything else' in the shared mapping — 'partial', never "
            "'complete'"
        )

        # Second half: the stages settle SUCCEEDED; the REAL atom sync
        # advances the live 'partial' to 'complete'.
        with pool.connection() as conn:
            for ordinal in (0, 1):
                atom_pk = conn.execute(
                    "SELECT id FROM document_atoms "
                    "WHERE file_id = ? AND ordinal = ?",
                    (file_id, ordinal),
                ).fetchone()["id"]
                completed = est.complete_atom_stage(
                    conn,
                    file_id=file_id,
                    generation_hash="gen1",
                    atom_pk=atom_pk,
                    stage=est.ENRICH_STAGE,
                    input_fingerprint=fingerprints[ordinal],
                    status=est.SUCCEEDED,
                    attempts=1,
                )
                assert completed, (
                    f"completing atom stage ordinal={ordinal} SUCCEEDED "
                    "failed"
                )
            conn.commit()

        _run_atom_sync(pool, file_id, vault_id)
        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "complete",
            None,
        ), (
            "atom sync did not settle the in-flight partial to complete: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} after both "
            "RUNNING stages completed SUCCEEDED and the REAL atom sync "
            "ran — the chunk arm's 'partial' is a live state the atom "
            "pipeline must be able to advance to 'complete'"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# C.4: a newer 'processing' re-stamp survives the older attempt's cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_write_does_not_clobber_newer_processing_restamp(
    tmp_path,
):
    """A newer attempt's 'processing' re-stamp survives an older cancel.

    The REAL job stamps 'processing' and parks; while parked, a NEWER job
    re-enters 'processing' directly in SQL with its OWN newer
    ``enrichment_updated_at`` (status 'processing', error NULL). The older
    attempt is then cancelled and its detached cancellation write drains.
    The cancellation CAS keys on THIS attempt's stamp, so the newer
    attempt's row must survive untouched: still 'processing', error NULL,
    ``enrichment_updated_at`` still exactly the newer stamp. RED at base
    (the unconditional cancel write makes it 'error'), GREEN after the
    fix.

    Post-fix mutation that turns this RED: a CAS that checks only
    ``enrichment_status='processing'`` but drops the
    ``enrichment_updated_at`` predicate.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="pending", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        stub, chunk = _make_job_stub(pool)
        task = _start_real_job(stub, file_id, vault_id, chunk)
        await _wait_for_status(pool, file_id, "processing")

        # A newer job re-enters 'processing' with its own newer stamp.
        newer_stamp = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE files SET enrichment_status = 'processing', "
                "enrichment_error = NULL, enrichment_updated_at = ? "
                "WHERE id = ?",
                (newer_stamp, file_id),
            )
            conn.commit()
        finally:
            conn.close()

        await _cancel_and_drain(stub, task)

        row = _full_row(pool, file_id)
        assert (
            row["enrichment_status"] == "processing"
            and row["enrichment_error"] is None
            and row["enrichment_updated_at"] == newer_stamp
        ), (
            "cancellation write clobbered a newer processing attempt: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} / "
            f"enrichment_updated_at={row['enrichment_updated_at']!r} "
            "after the OLDER attempt's detached cancellation task drained, "
            "but a NEWER job had re-entered 'processing' with stamp "
            f"{newer_stamp!r} — the cancellation CAS must key on the "
            "cancelled attempt's own stamp and therefore not fire"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# C.5 PRESERVING: hash-rotated mid-flight -> stale marker (writer 8)
# ---------------------------------------------------------------------------


class _HashRotatingBoomService:
    """Chunk-enrichment double: rotates file_hash mid-flight, then raises.

    Mirrors a re-ingest superseding the attempt while its LLM call is in
    flight: the files row's ``file_hash`` changes (status and enrichment
    stamps untouched), then the enrichment call fails.
    """

    def __init__(self, db_path: str, file_id: int) -> None:
        self._db_path = db_path
        self._file_id = file_id

    async def enrich_chunks(self, chunk_dicts, document_title: str = ""):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE files SET file_hash = 'rotated-hash' WHERE id = ?",
                (self._file_id,),
            )
            conn.commit()
        finally:
            conn.close()
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_except_handler_hash_rotated_mid_flight_marks_stale(tmp_path):
    """A hash rotation mid-flight marks the attempt stale, not failed.

    The REAL ``run_enrichment_job`` runs with a service double that
    rotates ``file_hash`` during ``enrich_chunks`` and then raises; the
    except-Exception handler (writer 8) must end the row at 'error' with
    the ``became stale`` marker (from
    ``_mark_enrichment_stale_if_current_job``) — the superseded attempt's
    outcome — and NOT the ENRICHMENT_FAILED code a current attempt would
    get. Pins that the atomic hash-CAS rework stays behavior-identical to
    today's check-then-write. GREEN at base and head.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="pending", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        stub, chunk = _make_job_stub(pool)
        service = _HashRotatingBoomService(db_path, file_id)
        stub._get_chunk_enrichment_service = lambda vault_id, file_id: service
        # The handler swallows the exception internally; the job returns.
        await _start_real_job(stub, file_id, vault_id, chunk)

        row = _enrichment_row(pool, file_id)
        assert row["enrichment_status"] == "error", (
            "a hash-rotated superseded attempt must end at 'error', got "
            f"{row['enrichment_status']!r} (enrichment_error="
            f"{row['enrichment_error']!r})"
        )
        assert row["enrichment_error"] and (
            _STALE_MARKER_TEXT in row["enrichment_error"]
        ), (
            "a hash-rotated superseded attempt must carry the stale marker "
            f"({_STALE_MARKER_TEXT!r}), got enrichment_error="
            f"{row['enrichment_error']!r}"
        )
        assert "ENRICHMENT_FAILED" not in row["enrichment_error"], (
            "a superseded (hash-rotated) attempt must NOT get the "
            "ENRICHMENT_FAILED code a current attempt would; got "
            f"enrichment_error={row['enrichment_error']!r}"
        )
    finally:
        pool.close_all()
