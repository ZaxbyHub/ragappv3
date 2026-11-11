"""Follow-up #1073 status-clobber EXTENSIONS (issues #1075 / #1076, wave 2).

Plan-critic amendments on top of the frozen
``test_followup_1073_status_clobber.py`` contracts — its harness is reused
by importing the sibling test module (the established backend/tests
pattern, e.g. test_d07b importing test_d07):

* (a) PRESERVING — the atom aggregate's all-``FAILED_PERMANENT``
  vocabulary ('error' with NULL ``enrichment_error``) still lands on a
  NULL-error row: the AC1 guard must only skip rows carrying a non-NULL
  error, never the aggregate's own error state. GREEN at base and after
  the fix.
* (b) DISCRIMINATING — the reverse direction (AC3, adjudicated): the chunk
  pipeline's early 'complete' writer (``run_enrichment_job``'s
  no-service-configured arm, document_processor.py ~2567-2570) must NOT
  overwrite an atom 'partial' aggregate that carries a FAILED_PERMANENT
  atom stage. RED at base, GREEN after the fix.
* (c) DISCRIMINATING — the orchestrator-selected residual pending
  maintainer review (decision D1) of the approved AC2 CAS shape:
  an atom aggregate that interleaves between a chunk attempt's
  'processing' stamp and its cancellation WINS over the later cancellation
  write (the CAS keys on this attempt's own 'processing' stamp, which the
  interleaved aggregate no longer matches). This pins the
  orchestrator-selected residual deliberately — any future provenance
  change that lets the cancellation write land over an interleaved atom
  aggregate must amend this spec consciously. RED at base, GREEN after
  the fix.
"""

import pytest
from test_followup_1073_status_clobber import (  # noqa: I001
    CANCEL_MESSAGE,
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


def _open_pool(db_path: str) -> SQLiteConnectionPool:
    """Open a pool on the seeded DB (mirror of the frozen file's pattern)."""
    return SQLiteConnectionPool(db_path)


# ---------------------------------------------------------------------------
# (a) PRESERVING: the all-failed atom aggregate still writes 'error'/NULL
# ---------------------------------------------------------------------------


def test_atom_sync_writes_error_aggregate_when_all_failed_permanent(tmp_path):
    """All-FAILED_PERMANENT atom stages aggregate to 'error' with NULL error.

    The atom aggregate's own failure vocabulary is
    ``enrichment_status='error'`` with a NULL ``enrichment_error`` — the
    guard added for AC1 (skip rows whose CURRENT error is non-NULL) must
    not confuse that with the chunk pipeline's failure row (which always
    carries a non-NULL message): on a NULL-error row the aggregate is the
    legitimate owner and must land.

    Mutation that turns this RED: widening the AC1 skip predicate to block
    writing 'error' aggregates at all (or to skip on any terminal status).
    GREEN at base and after the fix.
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
            f"precondition: expected 2 FAILED_PERMANENT stages, counts={counts!r}"
        )

        _run_atom_sync(pool, file_id, vault_id)

        row = _enrichment_row(pool, file_id)
        assert row["enrichment_status"] == "error", (
            "all-FAILED_PERMANENT atom aggregate must write "
            "enrichment_status='error' on a NULL-error row, got "
            f"{row['enrichment_status']!r} (enrichment_error="
            f"{row['enrichment_error']!r})"
        )
        assert row["enrichment_error"] is None, (
            "the atom aggregate's error vocabulary keeps enrichment_error "
            f"NULL, got {row['enrichment_error']!r}"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# (b) DISCRIMINATING: chunk early-'complete' must not erase an atom partial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_complete_yields_partial_when_an_atom_stage_failed_permanently(
    tmp_path,
):
    """The chunk pipeline's early 'complete' must not erase an atom partial.

    The row is seeded 'processing' / NULL error (an in-flight attempt's
    stamp — NOT a pre-existing 'partial'), over a [SUCCEEDED,
    FAILED_PERMANENT] stage set. The REAL ``run_enrichment_job`` runs with
    NO chunk-enrichment service configured, so it takes the early
    ``set_enrichment_status(file_id, "complete")`` arm
    (document_processor.py ~2567-2570). That write must compute the status
    from the atom stages — the only way to end at 'partial' here is
    reading them; an implementation that merely preserves a pre-existing
    'partial' row cannot pass this seeding. A FAILED_PERMANENT atom stage
    is durable evidence the file's enrichment is not complete. RED at base
    (the row becomes 'complete'), GREEN after the fix.

    Post-fix mutation that turns this RED: removing the atom-stage
    FAILED_PERMANENT check from the chunk 'complete' writers.
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
            f"precondition: expected 1 FAILED_PERMANENT stage, counts={counts!r}"
        )
        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "processing",
            None,
        ), f"precondition: seeded row is not processing/NULL: {dict(row)!r}"

        stub, chunk = _make_job_stub(pool)
        # No enrichment service configured -> run_enrichment_job takes the
        # early 'complete' arm before any candidate/model work.
        stub._get_chunk_enrichment_service = lambda vault_id, file_id: None
        task = _start_real_job(stub, file_id, vault_id, chunk)
        await task  # runs to completion; nothing is cancelled here

        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "partial",
            None,
        ), (
            "chunk complete overwrote atom partial aggregate: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} after the REAL "
            "run_enrichment_job early-complete arm ran with no enrichment "
            "service configured, but the file's atom stages are 1 SUCCEEDED "
            "+ 1 FAILED_PERMANENT and the shared stage mapping must yield "
            "'partial' (a FAILED_PERMANENT atom stage keeps the file's "
            "enrichment from being reported complete)"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# (c) DISCRIMINATING: D1 residual — interleaved aggregate beats cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_write_yields_to_interleaved_atom_aggregate(tmp_path):
    """An interleaved atom aggregate WINS over the attempt's later cancel.

    Sequence: the REAL chunk job stamps 'processing' and parks; while it
    is parked, the REAL atom sync settles an all-SUCCEEDED batch on the
    same NULL-error row (writing 'complete'/NULL with a stamp newer than
    the attempt's 'processing' stamp); the parked attempt is then
    cancelled and its detached cancellation write (carrying the
    "Enrichment job cancelled before completion" message) drains.

    The approved AC2 CAS keys on THIS attempt's own 'processing' stamp, so
    the cancellation payload must NOT land over the interleaved atom
    aggregate — the row stays 'complete'/NULL. THIS IS THE
    ORCHESTRATOR-SELECTED RESIDUAL PENDING MAINTAINER REVIEW (DECISION D1)
    of the CAS shape: an atom aggregate that interleaves between the
    attempt's stamp and its cancellation is silently kept, and the chunk
    attempt's cancellation is dropped. Any
    future provenance change that makes the cancellation write land over
    an interleaved aggregate must amend this spec consciously. RED at base
    (the unconditional cancel write clobbers 'complete'), GREEN after the
    fix.

    Post-fix mutation that turns this RED: loosening the detached
    cancellation CAS so the payload lands on the file row regardless of
    its current state (the pre-fix unconditional ``WHERE id=?`` write).
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="pending", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        stub, chunk = _make_job_stub(pool)
        task = _start_real_job(stub, file_id, vault_id, chunk)
        await _wait_for_status(pool, file_id, "processing")

        # While the chunk attempt is parked, the atom pipeline settles an
        # all-SUCCEEDED batch: the REAL atom sync writes 'complete'/NULL
        # with a stamp newer than the attempt's 'processing' stamp.
        counts = _seed_atom_stages(
            pool, file_id, [(0, est.SUCCEEDED), (1, est.SUCCEEDED)]
        )
        assert counts[est.SUCCEEDED] == 2, (
            f"precondition: expected 2 SUCCEEDED stages, counts={counts!r}"
        )
        _run_atom_sync(pool, file_id, vault_id)
        row = _enrichment_row(pool, file_id)
        assert row["enrichment_status"] == "complete", (
            "precondition: interleaved atom sync did not write 'complete' "
            f"(row={dict(row)!r})"
        )

        await _cancel_and_drain(stub, task)

        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "complete",
            None,
        ), (
            "interleaved atom aggregate was clobbered by the cancellation "
            f"write: files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} after the "
            "cancelled attempt's detached status task drained, but the "
            "interleaved atom aggregate ('complete'/NULL, stamped AFTER "
            "the attempt's 'processing' stamp) had taken the row — the "
            "cancellation CAS must key on this attempt's own 'processing' "
            "stamp and therefore not fire (accepted residual: the atom "
            "aggregate wins over the later cancel)"
        )
    finally:
        pool.close_all()
