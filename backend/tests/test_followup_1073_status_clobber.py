"""Follow-up #1073 enrichment-status clobber contracts (issues #1075 / #1076).

Two terminal ``files.enrichment_status`` writers survived PR #1073's guards:

* AC1 — ``BackgroundProcessor._sync_file_enrichment_status``
  (background_tasks.py ~2963-2988) maps the atom-stage aggregate onto the
  file row with an unconditional ``UPDATE files SET enrichment_status=?,
  enrichment_error=NULL, ... WHERE id=?``. The atom pipeline and the chunk
  pipeline share the file row (same ``file_hash``), so a settled atom batch
  landing after a chunk-enrichment failure rewrites the failure row to the
  atom aggregate — erasing the operator-visible ``enrichment_error``
  (documents.py stops reporting the chunk failure).
* AC2 — the ``except asyncio.CancelledError`` handler of
  ``DocumentProcessor.run_enrichment_job`` (document_processor.py
  ~2668-2686) schedules a detached
  ``set_enrichment_status(file_id, "error", "Enrichment job cancelled
  before completion")`` whose UPDATE (set_enrichment_status ~1572-1601)
  carries no currency predicate beyond ``WHERE id=?``: a superseded attempt
  cancelled AFTER a newer job wrote its terminal state clobbers the newer
  row. The #704 detached-survival property (the write still lands when the
  attempt IS current) must survive the fix.

These specs drive the REAL production functions against a real temp sqlite
DB: ``BackgroundProcessor._sync_file_enrichment_status`` is called directly
on a SimpleNamespace stub carrying only ``processor.pool`` (the probe
pattern), and the AC2 legs drive the REAL unbound
``DocumentProcessor.run_enrichment_job`` through its actual
``except asyncio.CancelledError`` handler (the detached task is scheduled
by the production code, not by the test) — the stub carries only the
collaborators the handler path touches (pool, write semaphore, detached
task set, a parking enrichment service, one candidate chunk). RED at base,
GREEN after the fix.

AC3 (the reverse chunk->atom direction) is intentionally not covered here:
its disposition is a product decision the plan critic adjudicates, and a
characterization test would either bless today's overwrite or guess the
unbuilt guard's shape.
"""

import asyncio
import functools
import os
import sqlite3
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover - environments without lancedb
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.models.database import SQLiteConnectionPool, init_db
from app.services import enrichment_state as est
from app.services.background_tasks import (  # noqa: I001
    AtomEnrichmentTaskItem,
    BackgroundProcessor,
)
from app.services.document_processor import DocumentProcessor

CHUNK_FAILURE_MESSAGE = "enrichment_failed (2 of 5 chunks)"
CANCEL_MESSAGE = "Enrichment job cancelled before completion"


# ---------------------------------------------------------------------------
# Harness (probe pattern: real sqlite DB, real production functions)
# ---------------------------------------------------------------------------


def _make_db(
    tmp_path: Path,
    *,
    enrichment_status: str = "error",
    enrichment_error: str | None = CHUNK_FAILURE_MESSAGE,
) -> tuple[str, int, int]:
    """init_db + one vault + one indexed file row in the given state."""
    db_path = str(Path(tmp_path) / "clobber.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (file_name, file_path, file_hash, file_size, "
            "vault_id, status, enrichment_status, enrichment_error, "
            "enrichment_updated_at) "
            "VALUES ('a.png', '/x/a.png', 'hash1', 0, ?, 'indexed', ?, ?, ?)",
            (
                vault_id,
                enrichment_status,
                enrichment_error,
                datetime.now(UTC).isoformat(),
            ),
        )
        file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return db_path, file_id, vault_id


def _enrichment_row(pool: SQLiteConnectionPool, file_id: int) -> sqlite3.Row:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT enrichment_status, enrichment_error FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()


def _seed_atom_stages(
    pool: SQLiteConnectionPool, file_id: int, specs: list[tuple[int, str]]
) -> dict[str, int]:
    """Seed one ENRICH_STAGE row per (ordinal, terminal status) spec.

    Goes through the REAL claim -> complete state machine so
    ``aggregate_stage_status`` genuinely counts the stages (a bare
    ``complete_atom_stage`` without a claim matches zero rows and leaves no
    stage behind — total==0 also aggregates to 'complete', which would make
    the seeding vacuous).
    """
    generation_hash = "gen1"
    with pool.connection() as conn:
        for ordinal, status in specs:
            atom_pk = conn.execute(
                "INSERT INTO document_atoms (atom_id, schema_version, file_id, "
                "generation_hash, ordinal, kind, raw_text) "
                "VALUES (?, 1, ?, ?, ?, 'image', 'ocr') RETURNING id",
                (f"atom-{ordinal}", file_id, generation_hash, ordinal),
            ).fetchone()["id"]
            fingerprint = f"fp-{ordinal}"
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
            completed = est.complete_atom_stage(
                conn,
                file_id=file_id,
                generation_hash=generation_hash,
                atom_pk=atom_pk,
                stage=est.ENRICH_STAGE,
                input_fingerprint=fingerprint,
                status=status,
                attempts=1,
            )
            assert claimed and completed, (
                f"seeding atom stage ordinal={ordinal} as {status} failed "
                f"(claimed={claimed}, completed={completed})"
            )
        conn.commit()
        counts = est.aggregate_stage_status(
            conn, file_id=file_id, stage=est.ENRICH_STAGE
        )
    return counts


def _run_atom_sync(pool: SQLiteConnectionPool, file_id: int, vault_id: int) -> None:
    """Call the REAL _sync_file_enrichment_status on the probe's stub shape."""
    stub = types.SimpleNamespace(processor=types.SimpleNamespace(pool=pool))
    item = AtomEnrichmentTaskItem(
        file_id=file_id,
        vault_id=vault_id,
        generation_hash="gen1",
        file_hash="hash1",
        document_title="a.png",
        attempt=0,
    )
    BackgroundProcessor._sync_file_enrichment_status(stub, item)


# ---------------------------------------------------------------------------
# AC1: atom sync must not overwrite a chunk-failure terminal row
# ---------------------------------------------------------------------------


def test_atom_sync_does_not_clobber_chunk_failure(tmp_path):
    """A settled all-SUCCEEDED atom batch must not erase a chunk failure.

    The files row sits in the chunk pipeline's terminal failure state
    (``enrichment_status='error'``, ``enrichment_error`` non-NULL — exactly
    what run_enrichment_job's failed-candidate write produces). The atom
    stages for the same file all SUCCEEDED, so the aggregate vocabulary says
    'complete'. The atom sync must SKIP this row: the failure vocabulary
    always writes a non-NULL ``enrichment_error`` while the atom aggregate
    always writes NULL, so the current row's non-NULL error is the
    provenance signal that a chunk-path terminal state owns the row.

    Post-fix mutation that turns this RED again: deleting the
    ``enrichment_error IS NOT NULL`` skip predicate from
    ``_sync_file_enrichment_status``'s UPDATE (the unconditional
    ``WHERE id=?`` write returns). RED at base, GREEN after the fix.
    """
    db_path, file_id, vault_id = _make_db(tmp_path)
    pool = SQLiteConnectionPool(db_path)
    try:
        counts = _seed_atom_stages(
            pool, file_id, [(0, est.SUCCEEDED), (1, est.SUCCEEDED)]
        )
        # Precondition: the aggregate genuinely sees two SUCCEEDED stages
        # (not the vacuous total==0 -> 'complete' arm).
        assert counts[est.SUCCEEDED] == 2
        assert sum(counts.values()) == 2

        _run_atom_sync(pool, file_id, vault_id)

        row = _enrichment_row(pool, file_id)
        assert (
            row["enrichment_status"] == "error"
            and row["enrichment_error"] == CHUNK_FAILURE_MESSAGE
        ), (
            "atom sync overwrote chunk failure: files.enrichment_status="
            f"{row['enrichment_status']!r} / enrichment_error="
            f"{row['enrichment_error']!r} after the atom aggregate synced, "
            "but the row was in the chunk pipeline's terminal failure state "
            f"('error' / {CHUNK_FAILURE_MESSAGE!r}) and the atom sync must "
            "skip rows carrying a non-NULL enrichment_error"
        )
    finally:
        pool.close_all()


def test_atom_sync_still_writes_aggregate_when_no_chunk_failure(tmp_path):
    """The guard must not over-block: NULL-error rows still get the aggregate.

    Two variants on rows whose ``enrichment_error`` IS NULL (no recorded
    failure — the atom aggregate is the legitimate owner of the row):

    * all-SUCCEEDED stages -> the aggregate 'complete' is written (from a
      'processing' row, i.e. the normal in-flight shape);
    * one SUCCEEDED + one FAILED_PERMANENT -> the aggregate 'partial' is
      written (from a NULL-error row).

    Mutation that turns this RED: making the guard block every write (e.g.
    dropping the ``enrichment_error IS NULL`` branch so the UPDATE never
    fires) — the aggregate must still land on rows without a recorded
    failure. GREEN at base and after the fix.
    """
    # Variant 1: all-SUCCEEDED aggregate -> 'complete'.
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir()
    db_path, file_id, vault_id = _make_db(
        v1_dir, enrichment_status="processing", enrichment_error=None
    )
    pool = SQLiteConnectionPool(db_path)
    try:
        counts = _seed_atom_stages(
            pool, file_id, [(0, est.SUCCEEDED), (1, est.SUCCEEDED)]
        )
        assert counts[est.SUCCEEDED] == 2
        _run_atom_sync(pool, file_id, vault_id)
        row = _enrichment_row(pool, file_id)
        assert row["enrichment_status"] == "complete", (
            "all-SUCCEEDED atom aggregate must still be written on rows with "
            f"NULL enrichment_error, got {row['enrichment_status']!r} "
            f"(enrichment_error={row['enrichment_error']!r})"
        )
        assert row["enrichment_error"] is None
    finally:
        pool.close_all()

    # Variant 2: mixed SUCCEEDED + FAILED_PERMANENT -> 'partial'.
    v2_dir = tmp_path / "v2"
    v2_dir.mkdir()
    db_path, file_id, vault_id = _make_db(
        v2_dir, enrichment_status="complete", enrichment_error=None
    )
    pool = SQLiteConnectionPool(db_path)
    try:
        counts = _seed_atom_stages(
            pool, file_id, [(0, est.SUCCEEDED), (1, est.FAILED_PERMANENT)]
        )
        assert counts[est.SUCCEEDED] == 1
        assert counts[est.FAILED_PERMANENT] == 1
        _run_atom_sync(pool, file_id, vault_id)
        row = _enrichment_row(pool, file_id)
        assert row["enrichment_status"] == "partial", (
            "mixed SUCCEEDED+FAILED_PERMANENT atom aggregate must still be "
            "written as 'partial' on rows with NULL enrichment_error, got "
            f"{row['enrichment_status']!r} "
            f"(enrichment_error={row['enrichment_error']!r})"
        )
        assert row["enrichment_error"] is None
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# AC2: the cancelled detached write must not clobber a newer terminal row
# ---------------------------------------------------------------------------


class _ParkingEnrichmentService:
    """Chunk-enrichment service double whose enrich_chunks parks forever.

    ``run_enrichment_job`` suspends inside the try block (at the
    ``enrich_chunks`` await), which is BEFORE ``_validate_chunk_sizes`` /
    ``_embed_with_cache`` / the vector swap — so no other collaborator is
    ever reached.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def enrich_chunks(self, chunk_dicts, document_title: str = ""):
        await self.release.wait()
        raise AssertionError("the parking service must never complete")


def _make_job_stub(pool: SQLiteConnectionPool):
    """A minimal DocumentProcessor-shaped stub with REAL method bindings.

    ``set_enrichment_status``, ``_mark_enrichment_stale_if_current_job`` and
    ``_is_enrichment_job_current`` are the REAL unbound methods bound onto
    the stub; only the LLM-side collaborators are fakes.
    """
    stub = types.SimpleNamespace(
        pool=pool,
        _write_semaphore=asyncio.Semaphore(1),
        _detached_status_tasks=set(),
    )
    stub.set_enrichment_status = functools.partial(
        DocumentProcessor.set_enrichment_status, stub
    )
    stub._mark_enrichment_stale_if_current_job = functools.partial(
        DocumentProcessor._mark_enrichment_stale_if_current_job, stub
    )
    stub._is_enrichment_job_current = functools.partial(
        DocumentProcessor._is_enrichment_job_current, stub
    )
    parking = _ParkingEnrichmentService()
    stub._get_chunk_enrichment_service = lambda vault_id, file_id: parking
    chunk = types.SimpleNamespace(
        text="a chunk body with real content",
        metadata={"chunk_scale": "default", "total_chunks": 1},
    )
    stub._candidate_chunks_for_enrichment = lambda file_id, chunks: [(chunk, "f1_0")]
    return stub, chunk


def _start_real_job(stub, file_id: int, vault_id: int, chunk) -> asyncio.Task:
    """Run the REAL run_enrichment_job on the stub as a cancellable task."""
    return asyncio.ensure_future(
        DocumentProcessor.run_enrichment_job(
            stub,
            file_id=file_id,
            file_path="/x/a.png",
            vault_id=vault_id,
            file_hash="hash1",
            chunks=[chunk],
            document_text="a chunk body with real content",
        )
    )


async def _wait_for_status(
    pool: SQLiteConnectionPool, file_id: int, status: str, timeout_s: float = 5.0
) -> None:
    """Poll the row until enrichment_status reaches ``status``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _enrichment_row(pool, file_id)["enrichment_status"] == status:
            return
        await asyncio.sleep(0.02)
    row = _enrichment_row(pool, file_id)
    pytest.fail(
        f"files.enrichment_status never reached {status!r} within "
        f"{timeout_s}s (stuck at {row['enrichment_status']!r})"
    )


async def _cancel_and_drain(stub, task: asyncio.Task) -> None:
    """Cancel the job, await its CancelledError, then drain detached tasks."""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for detached in list(stub._detached_status_tasks):
        await detached
    # Let any done-callbacks (set.discard) run before the assertions read.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_detached_write_does_not_clobber_newer_row(tmp_path):
    """A cancelled superseded attempt must not clobber the newer terminal row.

    Drives the REAL ``run_enrichment_job`` through its actual
    ``except asyncio.CancelledError`` handler: the job stamps 'processing',
    parks inside ``enrich_chunks``; while it is parked a NEWER job writes
    its terminal state ('complete' / NULL error / a strictly newer
    ``enrichment_updated_at``); then the parked attempt is cancelled and the
    production handler schedules the real detached
    ``set_enrichment_status(file_id, "error", "Enrichment job cancelled
    before completion")``. The detached write must NOT land on the newer
    job's row.

    Post-fix mutation that turns this RED again: replacing the detached
    task's CAS UPDATE with the raw ``WHERE id=?`` UPDATE (the pre-fix
    shape). RED at base (the raw UPDATE clobbers 'complete'), GREEN after
    the fix.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="pending", enrichment_error=None
    )
    pool = SQLiteConnectionPool(db_path)
    try:
        stub, chunk = _make_job_stub(pool)
        task = _start_real_job(stub, file_id, vault_id, chunk)
        await _wait_for_status(pool, file_id, "processing")

        # The newer job writes its terminal state directly, with a strictly
        # NEWER stamp than this attempt's 'processing' stamp.
        newer_stamp = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE files SET enrichment_status = 'complete', "
                "enrichment_error = NULL, enrichment_updated_at = ? "
                "WHERE id = ?",
                (newer_stamp, file_id),
            )
            conn.commit()
        finally:
            conn.close()

        await _cancel_and_drain(stub, task)

        row = _enrichment_row(pool, file_id)
        assert (
            row["enrichment_status"] == "complete"
            and row["enrichment_error"] is None
        ), (
            "cancelled detached write clobbered newer row: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r} after the "
            "cancelled attempt's detached status task drained, but a newer "
            "job had already written its terminal state ('complete' / NULL) "
            "with a newer enrichment_updated_at — the cancellation write "
            "must be scoped to this attempt's own 'processing' row"
        )
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_cancelled_detached_write_still_lands_when_attempt_is_current(
    tmp_path,
):
    """#704 survival: the detached cancellation write still lands.

    Same flow with NO newer job: the attempt still owns the row when it is
    cancelled, so the detached ``set_enrichment_status`` write MUST land —
    ``error`` with the 'Enrichment job cancelled before completion' message.
    A fix that swallows the cancellation write entirely (or keys its CAS on
    a state this attempt no longer holds) turns this RED.

    GREEN at base and after the fix.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="pending", enrichment_error=None
    )
    pool = SQLiteConnectionPool(db_path)
    try:
        stub, chunk = _make_job_stub(pool)
        task = _start_real_job(stub, file_id, vault_id, chunk)
        await _wait_for_status(pool, file_id, "processing")

        await _cancel_and_drain(stub, task)

        row = _enrichment_row(pool, file_id)
        assert row["enrichment_status"] == "error", (
            "the detached cancellation write must still land when the "
            "cancelled attempt is the current one (#704), got "
            f"enrichment_status={row['enrichment_status']!r}"
        )
        assert row["enrichment_error"] and (
            "cancelled before completion" in row["enrichment_error"]
        ), (
            "the detached cancellation write must carry the cancellation "
            f"message, got enrichment_error={row['enrichment_error']!r}"
        )
    finally:
        pool.close_all()
