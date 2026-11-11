"""Follow-up #1073 status-clobber EXTENSIONS, wave 4 (critic Round 3).

Focused gaps on the approved design: the EXISTING
``DocumentProcessor.set_enrichment_status`` computes the 'complete' status
via a correlated SQL CASE over ``ingestion_stage_states`` ( correlated per
file: ``s.file_id = files.id AND s.stage='enrich' AND s.atom_id IS NOT
NULL``), and guarded writes carry ``require_file_hash`` (UPDATE ...
``AND file_hash=? AND status IN ('indexed','partial')``), returning the
updated_at ISO string on write, the module-level ``ENRICHMENT_WRITE_NOT_
CURRENT`` sentinel string when the guard matched zero rows, and ``None``
ONLY on sqlite error / missing pool. Stale-marker fallbacks fire only on
the sentinel, never on ``None``. The frozen harness is imported from
``test_followup_1073_status_clobber``; nothing here needs a binding it
does not provide.

* D.1 DISCRIMINATING — the complete-CASE is correlated PER FILE: a
  sibling file's failed atom stages must not leak into this file's
  aggregate. RED at base, GREEN after the fix.
* D.2 DISCRIMINATING — the early-complete arm must not write over a row
  whose hash rotated between the entry currency check and the write; the
  early arms have no 'processing' stamp, so NOT_CURRENT means a silent
  skip (the row is owned by the newer ingest). RED at base, GREEN after
  the fix.
* D.3 PRESERVING — a sqlite failure on the writer-8 guarded error write
  must NOT trigger the stale-marker fallback (the None return is not the
  NOT_CURRENT sentinel). GREEN at base and head.
"""

import sqlite3
from datetime import UTC, datetime

import pytest
from test_followup_1073_status_clobber import (  # noqa: I001
    _enrichment_row,
    _make_db,
    _make_job_stub,
    _seed_atom_stages,
    _start_real_job,
)

from app.models.database import SQLiteConnectionPool
from app.services import enrichment_state as est
from app.services.document_processor import DocumentProcessor


def _open_pool(db_path: str) -> SQLiteConnectionPool:
    """Open a pool on the seeded DB (mirror of the frozen file's pattern)."""
    return SQLiteConnectionPool(db_path)


def _insert_second_file(db_path: str, vault_id: int, *, file_hash: str) -> int:
    """Insert a second indexed files row (processing/NULL); return its id."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO files (file_name, file_path, file_hash, file_size, "
            "vault_id, status, enrichment_status, enrichment_error, "
            "enrichment_updated_at) "
            "VALUES ('b.png', '/x/b.png', ?, 0, ?, 'indexed', 'processing', "
            "NULL, ?)",
            (file_hash, vault_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM files WHERE file_hash = ?", (file_hash,)
        ).fetchone()[0]
    finally:
        conn.close()


async def _run_early_complete(stub, *, file_id, vault_id, chunk, file_hash,
                              file_path):
    """Drive the REAL run_enrichment_job early-complete arm for one file."""
    await DocumentProcessor.run_enrichment_job(
        stub,
        file_id=file_id,
        file_path=file_path,
        vault_id=vault_id,
        file_hash=file_hash,
        chunks=[chunk],
        document_text="body",
    )


# ---------------------------------------------------------------------------
# D.1: the complete-CASE is correlated per file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_case_is_correlated_per_file(tmp_path):
    """A sibling file's failed atom stages must not leak into the aggregate.

    TWO files in ONE db: file A (hash1) has NO atom stages; file B (hash2)
    has two FAILED_PERMANENT ENRICH_STAGE rows (ordinals 10/11 so the
    document_atoms.atom_id strings stay distinct). The REAL
    ``run_enrichment_job`` early-complete arm runs for A (hash1) -> A must
    read 'complete' (zero stage rows), then for B (hash2) -> B must read
    'error' (all failed_permanent). RED at base (both arms write a blind
    'complete', so B is wrong), GREEN after the fix.

    Post-fix mutation that turns this RED: an uncorrelated CASE that
    aggregates the stage rows across files (dropping the
    ``s.file_id = files.id`` correlation) makes file A read 'error'.
    """
    db_path, file_a, vault_id = _make_db(
        tmp_path, enrichment_status="processing", enrichment_error=None
    )
    file_b = _insert_second_file(db_path, vault_id, file_hash="hash2")
    pool = _open_pool(db_path)
    try:
        counts = _seed_atom_stages(
            pool, file_b, [(10, est.FAILED_PERMANENT), (11, est.FAILED_PERMANENT)]
        )
        assert counts[est.FAILED_PERMANENT] == 2, (
            f"precondition: expected 2 FAILED_PERMANENT stages on file B, "
            f"counts={counts!r}"
        )
        with pool.connection() as conn:
            counts_a = est.aggregate_stage_status(
                conn, file_id=file_a, stage=est.ENRICH_STAGE
            )
        assert sum(counts_a.values()) == 0, (
            f"precondition: file A must have zero stage rows, "
            f"counts={counts_a!r}"
        )

        stub, chunk = _make_job_stub(pool)
        stub._get_chunk_enrichment_service = lambda vault_id, file_id: None
        await _run_early_complete(
            stub, file_id=file_a, vault_id=vault_id, chunk=chunk,
            file_hash="hash1", file_path="/x/a.png",
        )
        await _run_early_complete(
            stub, file_id=file_b, vault_id=vault_id, chunk=chunk,
            file_hash="hash2", file_path="/x/b.png",
        )

        row_a = _enrichment_row(pool, file_a)
        row_b = _enrichment_row(pool, file_b)
        assert (
            row_a["enrichment_status"] == "complete"
            and row_a["enrichment_error"] is None
            and row_b["enrichment_status"] == "error"
            and row_b["enrichment_error"] is None
        ), (
            "complete CASE is not correlated per file: file A (hash1, zero "
            f"atom stages) read {row_a['enrichment_status']!r}/"
            f"{row_a['enrichment_error']!r} (expected 'complete'/None) and "
            "file B (hash2, two FAILED_PERMANENT stages) read "
            f"{row_b['enrichment_status']!r}/{row_b['enrichment_error']!r} "
            "(expected 'error'/None) — the CASE must aggregate each file's "
            "OWN stage rows only, never the sibling's"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# D.2: early-complete arm vs a hash rotated between precheck and write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_arm_hash_rotated_between_precheck_and_write_marks_stale(
    tmp_path,
):
    """The early-complete write must not land over a rotated-hash row.

    ``_get_chunk_enrichment_service`` runs BETWEEN run_enrichment_job's
    entry ``_is_enrichment_job_current`` check (:2564) and the early
    complete write (:2569): the override rotates ``file_hash`` to
    'rotated-hash' via direct SQL and then returns None, so the arm fires
    with the row already owned by a newer ingest. The early arms run
    BEFORE the 'processing' stamp (:2578), so there is no
    ``processing_started_at`` — on the NOT_CURRENT sentinel the correct
    outcome is a SILENT SKIP (the stale marker has no stamp to key on;
    the row is owned by the newer ingest). The row must keep its seeded
    pre-state ('pending'/NULL), never 'complete'. RED at base (the
    ID-only write lands), GREEN after the fix.

    Post-fix mutation that turns this RED: dropping the
    ``require_file_hash`` guard from the early-complete write (the
    pre-fix ``WHERE id=?`` UPDATE).
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="pending", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        stub, chunk = _make_job_stub(pool)

        def _rotate_hash_then_no_service(vault_id, file_id):
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE files SET file_hash = 'rotated-hash' WHERE id = ?",
                    (file_id,),
                )
                conn.commit()
            finally:
                conn.close()
            return None

        stub._get_chunk_enrichment_service = _rotate_hash_then_no_service
        task = _start_real_job(stub, file_id, vault_id, chunk)
        await task  # runs to completion; nothing is cancelled

        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "pending",
            None,
        ), (
            "early complete arm wrote over a rotated-hash row: "
            f"files.enrichment_status={row['enrichment_status']!r} / "
            f"enrichment_error={row['enrichment_error']!r}, but file_hash "
            "was rotated to 'rotated-hash' between the entry currency "
            "check and the write — the guarded complete write must match "
            "zero rows, and the early arms (no 'processing' stamp) must "
            "silently skip, leaving the seeded 'pending'/NULL row for the "
            "newer ingest that owns it"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# D.3 PRESERVING: sqlite failure on the guarded error write -> no fallback
# ---------------------------------------------------------------------------


class _BoomService:
    """Chunk-enrichment double whose enrich_chunks raises immediately."""

    async def enrich_chunks(self, chunk_dicts, document_title: str = ""):
        raise RuntimeError("boom")


class _ErrorWriteFailingConnection:
    """Connection proxy: the terminal 'error' status write fails, locked.

    ``execute`` raises ``sqlite3.OperationalError("database is locked")``
    only when the SQL touches ``enrichment_status`` AND the bound params
    carry the literal ``'error'`` status (the writer-8 terminal write).
    Everything else — the 'processing' stamp, the currency SELECT, a
    hypothetical stale-marker write — delegates to the real connection.
    """

    def __init__(self, real_conn):
        self._real_conn = real_conn

    def execute(self, sql, params=()):
        if "enrichment_status" in sql and params and "error" in tuple(params):
            raise sqlite3.OperationalError("database is locked")
        return self._real_conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real_conn, name)


class _FailingErrorWritePool:
    """Pool wrapper handing write_session the failing connection proxy.

    Preserves the real pool's checkout bookkeeping (release_connection
    returns the underlying real connection); the sync ``connection()``
    context manager (used by ``_is_enrichment_job_current``) delegates
    untouched.
    """

    def __init__(self, real_pool):
        self._real = real_pool

    async def get_connection_async(self, *args, **kwargs):
        real_conn = await self._real.get_connection_async(*args, **kwargs)
        return _ErrorWriteFailingConnection(real_conn)

    def release_connection(self, conn):
        self._real.release_connection(getattr(conn, "_real_conn", conn))

    def connection(self):
        return self._real.connection()

    def close_all(self):
        self._real.close_all()


@pytest.mark.asyncio
async def test_guarded_error_write_sqlite_failure_does_not_mark_stale(tmp_path):
    """A sqlite failure on the guarded error write is NOT a stale fallback.

    The REAL job stamps 'processing' (that write delegates and succeeds),
    then the enrichment call raises RuntimeError; the writer-8 terminal
    ``set_enrichment_status(file_id, "error", ...)`` goes through a
    connection proxy whose ``execute`` raises
    ``sqlite3.OperationalError("database is locked")`` for exactly that
    write. ``set_enrichment_status`` swallows the error and returns None
    — which is NOT the ``ENRICHMENT_WRITE_NOT_CURRENT`` sentinel — so the
    caller must NOT run the stale-marker fallback. The row keeps its
    'processing' stamp with a NULL error. GREEN at base (today the
    sqlite error is swallowed inside set_enrichment_status and writer 8
    does nothing further) and at head (None is not the sentinel).

    Mutation that turns this RED (head): treating the None (sqlite-error)
    return like the NOT_CURRENT sentinel and calling the stale marker —
    the marker write itself delegates through the proxy and would land
    'error'/'became stale'.
    """
    db_path, file_id, vault_id = _make_db(
        tmp_path, enrichment_status="pending", enrichment_error=None
    )
    pool = _open_pool(db_path)
    try:
        stub, chunk = _make_job_stub(pool)
        stub._get_chunk_enrichment_service = (
            lambda vault_id, file_id: _BoomService()
        )
        # The real methods are bound to the stub via functools.partial and
        # read stub.pool at CALL time, so the wrapper takes effect for the
        # write_session path while the currency SELECT keeps the real pool.
        stub.pool = _FailingErrorWritePool(pool)

        task = _start_real_job(stub, file_id, vault_id, chunk)
        await task  # the handler swallows the RuntimeError

        row = _enrichment_row(pool, file_id)
        assert (row["enrichment_status"], row["enrichment_error"]) == (
            "processing",
            None,
        ), (
            "sqlite failure on the guarded error write triggered the stale "
            f"fallback: files.enrichment_status={row['enrichment_status']!r}"
            f" / enrichment_error={row['enrichment_error']!r}, but the "
            "terminal error write failed with "
            "sqlite3.OperationalError('database is locked') — a swallowed "
            "sqlite failure returns None (not the NOT_CURRENT sentinel) "
            "and must leave the row at its 'processing' stamp without any "
            "stale-marker fallback"
        )
    finally:
        pool.close_all()
