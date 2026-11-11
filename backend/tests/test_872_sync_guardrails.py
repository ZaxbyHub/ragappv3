"""Sync guardrails for the #872 latest-job fix (non-frozen).

Boundary matrix over the boot job sync's capped-failure exclusion (both the
mint and cap-settle arms) plus the fenced-settle gate: the capped-failure
exclusion keys on the file's LATEST ingestion job (a capped failure only
excludes/settles when no later ingestion job supersedes it), a live
pending/running job covers its file, NULL file_id rows never supersede or
match, and a lease-terminal settle that lost its fence writes no files row
(PRR parity with the reindex settle's fenced-off handling).

Superseded scope note: the #819 payload-parity and #820 dimension-refusal
settle-matrix guardrails that shared this file landed via PR #1107 (its
test_819_reindex_settlement_parity.py / test_820_dimension_no_retry.py);
this file keeps only the sync + fence-gate surface this PR owns.

Harness facts mirror test_b13_lease_reindex_durability.py: real schema via
run_migrations, SQLiteConnectionPool, real sync core, doubles only at the
documented seams.
"""

import json
import sqlite3

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.job_lease import ensure_jobs_schema

INGESTION_QUEUE = "ingestion"


def _seed(tmp_path, name):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return db_path, conn


# ---- Fix C boundary matrix -------------------------------------------------


def _insert_ingest_job(
    conn, file_id, *, status, attempts=0, payload_file_id=True
):
    payload = {
        "file_path": "uploads/bm.txt",
        "vault_id": 1,
        "source": "upload",
    }
    if payload_file_id:
        payload["file_id"] = file_id
    conn.execute(
        "INSERT INTO jobs (queue, payload_json, status, attempts) "
        "VALUES (?, ?, ?, ?)",
        (INGESTION_QUEUE, json.dumps(payload), status, attempts),
    )
    conn.commit()


def _sync_state(tmp_path, *, seed):
    db_path, conn = _seed(tmp_path, "bm.db")
    file_id = seed(conn)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    bp = BackgroundProcessor(pool=pool)
    created = bp._sync_missing_ingest_job_rows()  # noqa: SLF001
    pool.close_all()
    fresh = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE queue=? AND status='pending' "
        "AND CAST(json_extract(payload_json, '$.file_id') AS INTEGER)=?",
        (INGESTION_QUEUE, file_id),
    ).fetchone()["n"]
    frow = conn.execute(
        "SELECT status, error_message FROM files WHERE id=?", (file_id,)
    ).fetchone()
    conn.close()
    return created, fresh, dict(frow)


def test_boundary_matrix_latest_capped_still_excluded_and_settled(tmp_path):
    """#702 contract preserved: latest-is-capped never mints, settles."""

    def seed(conn):
        cap = int(settings.jobs_max_attempts)
        fid = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('bm.txt', 'bm.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        conn.commit()
        _insert_ingest_job(conn, fid, status="failed", attempts=cap)
        return fid

    created, fresh, frow = _sync_state(tmp_path, seed=seed)
    assert created == 0 and fresh == 0
    assert frow["status"] == "error"
    assert frow["error_message"] == "lease_attempt_cap_exceeded"


def test_boundary_matrix_older_cap_newer_pending_running_covered(tmp_path):
    """A pending/running successor job already covers the file: no mint."""

    def seed(conn):
        cap = int(settings.jobs_max_attempts)
        fid = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('bm.txt', 'bm.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        conn.commit()
        _insert_ingest_job(conn, fid, status="failed", attempts=cap)
        _insert_ingest_job(conn, fid, status="running", attempts=1)
        return fid

    created, fresh, frow = _sync_state(tmp_path, seed=seed)
    # The mint anti-join's pending/running coverage still excludes the file;
    # the cap-settle arm must NOT settle it (a live job owns it).
    assert created == 0
    assert frow["status"] == "pending", (
        "a file covered by a live job must not be cap-settled"
    )


def test_boundary_matrix_multiple_capped_rows_latest_decides(tmp_path):
    """Multiple capped failures then a completed job: recovery (C5 cohort);
    multiple capped with the LATEST capped: still excluded."""

    def seed(conn):
        cap = int(settings.jobs_max_attempts)
        fid = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('bm.txt', 'bm.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        conn.commit()
        _insert_ingest_job(conn, fid, status="failed", attempts=cap)
        _insert_ingest_job(conn, fid, status="failed", attempts=cap)
        _insert_ingest_job(conn, fid, status="completed", attempts=1)
        return fid

    created, fresh, frow = _sync_state(tmp_path, seed=seed)
    assert fresh == 1, "older capped rows must not outvote the completed job"
    assert frow["status"] == "pending"

    # Latest-is-capped variant in a fresh DB.

    def seed_latest(conn):
        cap = int(settings.jobs_max_attempts)
        fid = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('bm.txt', 'bm.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        conn.commit()
        _insert_ingest_job(conn, fid, status="completed", attempts=1)
        _insert_ingest_job(conn, fid, status="failed", attempts=cap)
        return fid

    created2, fresh2, frow2 = _sync_state(tmp_path / "latest", seed=seed_latest)
    assert created2 == 0 and fresh2 == 0
    assert frow2["status"] == "error"
    assert frow2["error_message"] == "lease_attempt_cap_exceeded"


def test_boundary_matrix_null_file_id_never_supersedes(tmp_path):
    """A later NULL-file_id job cannot supersede a capped row (NULL rows
    are dropped from the latest-job set by the IS NOT NULL filter), and a
    capped row whose file_id is NULL matches no file (same filter, every
    arm)."""

    def seed(conn):
        cap = int(settings.jobs_max_attempts)
        fid = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('bm.txt', 'bm.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        conn.commit()
        _insert_ingest_job(conn, fid, status="failed", attempts=cap)
        # Later job with NO file_id in its payload (foreign/legacy row).
        _insert_ingest_job(conn, fid, status="completed", attempts=1,
                           payload_file_id=False)
        return fid

    created, fresh, frow = _sync_state(tmp_path, seed=seed)
    assert created == 0 and fresh == 0, (
        "a NULL-file_id row must not supersede the capped failure"
    )
    assert frow["status"] == "error"
    assert frow["error_message"] == "lease_attempt_cap_exceeded"

    # Second leg: a capped row whose OWN payload has no file_id must not
    # suppress minting for any file (the IS NOT NULL filter drops it).
    def seed_null_capped(conn):
        fid = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('bm2.txt', 'bm2.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        conn.commit()
        # Capped failure with NO file_id in its payload.
        _insert_ingest_job(conn, fid, status="failed",
                           attempts=int(settings.jobs_max_attempts),
                           payload_file_id=False)
        return fid

    created2, fresh2, frow2 = _sync_state(tmp_path / "null-capped",
                                          seed=seed_null_capped)
    assert fresh2 == 1, (
        "a capped row with a NULL file_id must not match any file — the "
        "reset file still mints fresh work"
    )
    assert frow2["status"] == "pending"


# ---- Fenced-settle gate (Copilot finding, PR #1106 round 1) -----------------


async def test_fenced_terminal_settle_writes_no_files_row(tmp_path, monkeypatch):
    """A lease lost at the terminal settle must not demote the files row.

    When _settle_ingest_job('fail') is fenced off (returns False - the
    janitor/successor owns the durable row), the transport must skip
    _mark_task_permanently_failed entirely: a files-row write from a worker
    that lost the lease could demote a successor's in-flight 'processing'
    row. GREEN when the cap-arm gate exists; deleting the cap-arm gate
    makes this leg RED via the files row flipping to error (the probe
    raises an ordinary RuntimeError, so the cap arm is the one that runs;
    the dimension arm's gate is exercised by the leg below).
    """
    import json as _json

    from app.services.job_lease import JobLease

    db_path, conn = _seed(tmp_path, "fence.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.01)
        file_id = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, 'uploads/fence.txt', "
            "'fence.txt', 1, 'processing', NULL, 'upload')"
        ).lastrowid
        conn.commit()
        ensure_jobs_schema(conn)
        cur = conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, 'pending')",
            (
                INGESTION_QUEUE,
                _json.dumps(
                    {
                        "file_path": "uploads/fence.txt",
                        "vault_id": 1,
                        "source": "upload",
                        "file_id": file_id,
                    }
                ),
            ),
        )
        job_id = int(cur.lastrowid)
        cap = int(settings.jobs_max_attempts)
        conn.execute("UPDATE jobs SET attempts = ? WHERE id = ?", (cap - 1, job_id))
        conn.commit()

        async def fenced_core(task):  # noqa: ARG001
            raise RuntimeError("fence probe transient")

        # The settlement itself is fenced off: the durable row was reclaimed
        # between claim and settle (returns False like JobLease on fence loss).
        async def fenced_settle(job_id_, worker_id_, action, **kwargs):  # noqa: ARG001
            return False

        monkeypatch.setattr(processor, "_run_task_processing", fenced_core)
        monkeypatch.setattr(processor, "_settle_ingest_job", fenced_settle)

        async with pool.connection_async() as aconn:
            lease = JobLease(
                aconn,
                reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
                max_attempts=cap,
            )
            row = lease.claim(INGESTION_QUEUE, "fence-worker")
            await processor._process_ingest_job_row(row, "fence-worker")  # noqa: SLF001

        frow = conn.execute(
            "SELECT status FROM files WHERE id=?", (file_id,)
        ).fetchone()
        assert frow["status"] == "processing", (
            "a fenced-off terminal settle must NOT write the files row from "
            "the stale worker (the successor owns it), got "
            f"status={frow['status']!r}"
        )
    finally:
        conn.close()
        pool.close_all()


# ---- Dimension-arm fence gate + #954 malformed-payload tolerance ------------


async def test_fenced_dimension_settle_writes_no_files_row(tmp_path, monkeypatch):
    """The dimension arm's fence gate: lease lost at the refusal settle.

    The round-1 fence probe raises an ordinary RuntimeError (cap arm); this
    leg drives EmbeddingDimensionChangedError so the DIMENSION arm's
    settled-gate is the one exercised.
    """
    import json as _json

    from app.services.document_processor import EmbeddingDimensionChangedError
    from app.services.job_lease import JobLease

    db_path, conn = _seed(tmp_path, "fence-dim.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.01)
        file_id = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, 'uploads/fd.txt', "
            "'fd.txt', 1, 'processing', NULL, 'upload')"
        ).lastrowid
        conn.commit()
        ensure_jobs_schema(conn)
        cur = conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, 'pending')",
            (
                INGESTION_QUEUE,
                _json.dumps(
                    {
                        "file_path": "uploads/fd.txt",
                        "vault_id": 1,
                        "source": "upload",
                        "file_id": file_id,
                    }
                ),
            ),
        )
        job_id = int(cur.lastrowid)
        conn.commit()

        async def refusing_core(task):  # noqa: ARG001
            raise EmbeddingDimensionChangedError("embedding dimension 4 != 6")

        async def fenced_settle(job_id_, worker_id_, action, **kwargs):  # noqa: ARG001
            return False

        monkeypatch.setattr(processor, "_run_task_processing", refusing_core)
        monkeypatch.setattr(processor, "_settle_ingest_job", fenced_settle)

        async with pool.connection_async() as aconn:
            lease = JobLease(
                aconn,
                reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
                max_attempts=int(settings.jobs_max_attempts),
            )
            row = lease.claim(INGESTION_QUEUE, "fence-dim-worker")
            await processor._process_ingest_job_row(row, "fence-dim-worker")  # noqa: SLF001

        frow = conn.execute(
            "SELECT status FROM files WHERE id=?", (file_id,)
        ).fetchone()
        assert frow["status"] == "processing", (
            "a fenced-off dimension settle must NOT write the files row "
            f"from the stale worker, got status={frow['status']!r}"
        )
    finally:
        conn.close()
        pool.close_all()


def test_sync_tolerates_malformed_payload_rows(tmp_path):
    """#954 contract, scoped to this PR's new scan: a malformed TERMINAL
    payload_json row never aborts the latest-job-id set.

    The latest-job-id set scans every ingestion row (the latest job may be
    completed/cancelled), so a malformed terminal row must be dropped by
    the json_valid guards instead of raising OperationalError inside
    BEGIN IMMEDIATE and aborting the whole boot sync (delta-review finding,
    reproduced on the unguarded form). Cohort expectations alongside the
    malformed row: latest-capped still excluded+settled (#702), and a
    superseded-capped cohort still recovers. Scope note: malformed rows in
    pending/running status still abort via the PRE-EXISTING unguarded
    coverage anti-joins (same on master) — recorded in the PR body's
    Considered table.
    """

    def seed(conn):
        cap = int(settings.jobs_max_attempts)
        # Cohort A: capped-failed LATEST (valid payload) -> still excluded
        # and settled even with a malformed row elsewhere in the table.
        fid_a = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('a.txt', 'a.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        # Cohort B: capped failure superseded by a later COMPLETED job ->
        # recovers despite the malformed row in the same scan.
        fid_b = conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, "
            "phase, source, file_size) "
            "VALUES ('b.txt', 'b.txt', 1, 'pending', 'queued', 'upload', 10)"
        ).lastrowid
        conn.commit()
        _insert_ingest_job(conn, fid_a, status="failed", attempts=cap)
        _insert_ingest_job(conn, fid_b, status="failed", attempts=cap)
        _insert_ingest_job(conn, fid_b, status="completed", attempts=1)
        # A malformed TERMINAL row with NO file_id that the latest-job scan
        # must tolerate (planted raw, uncommitted -> commit before sync).
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) "
            "VALUES (?, '{not-json', 'completed')",
            (INGESTION_QUEUE,),
        )
        conn.commit()
        return fid_a, fid_b

    db_path, conn = _seed(tmp_path, "mal.db")
    file_a, file_b = seed(conn)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        bp = BackgroundProcessor(pool=pool)
        created = bp._sync_missing_ingest_job_rows()  # noqa: SLF001
        pool.close_all()

        fresh_a = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE queue=? AND status='pending' "
            "AND CAST(json_extract(payload_json, '$.file_id') AS INTEGER)=?",
            (INGESTION_QUEUE, file_a),
        ).fetchone()["n"]
        fresh_b = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE queue=? AND status='pending' "
            "AND CAST(json_extract(payload_json, '$.file_id') AS INTEGER)=?",
            (INGESTION_QUEUE, file_b),
        ).fetchone()["n"]
        row_a = conn.execute(
            "SELECT status, error_message FROM files WHERE id=?", (file_a,)
        ).fetchone()
        row_b = conn.execute(
            "SELECT status FROM files WHERE id=?", (file_b,)
        ).fetchone()
        assert created == 1 and fresh_b == 1, (
            "the malformed terminal row must be skipped by the json_valid "
            "guards, not abort the sync - the superseded cohort still recovers"
        )
        assert fresh_a == 0
        assert row_a["status"] == "error"
        assert row_a["error_message"] == "lease_attempt_cap_exceeded"
        assert row_b["status"] == "pending"
    finally:
        conn.close()
