"""Issue #872 follow-up acceptance checks — latest-job recovery vs stale cap.

``BackgroundProcessor._sync_missing_ingest_job_rows`` mints fresh pending
ingestion jobs for job-less recoverable files, with an anti-join that
excludes files whose ingestion retry budget was exhausted (issue #702
P02-SK2-08). The anti-join matches ANY ``jobs`` row with
``status='failed' AND attempts >= jobs_max_attempts`` for the file — even
when a LATER job for the same file COMPLETED (the file genuinely re-ingested
and indexed after the old capped failure). A file like that, once reset to
``pending``/``queued`` (the exact cohort ``scripts/reset_embeddings.py``
produces for its "Background processor will auto-reprocess the N reset
file(s)" promise), is treated as cap-exhausted: the sync mints NO fresh job
and instead settles the files row to ``error`` /
``lease_attempt_cap_exceeded``.

Checks (issue-tracer arm's-length acceptance spec):
* C5/AC5 (DISCRIMINATING — RED at base, GREEN after the fix): a file whose
  LATEST ingestion job completed successfully (with an older capped-failed
  job) is treated as job-less by the sync: after a reset to
  pending/queued, the sync mints a fresh pending job and the file does NOT
  settle error/lease_attempt_cap_exceeded. At base the stale cap anti-join
  suppresses the mint and settles the error — both assertions fail.
* C6/AC6 (DISCRIMINATING — RED at base, GREEN after the fix): the
  reset-script cohort promise. The REAL reset core from
  ``scripts/reset_embeddings.py`` (``reset_all_embeddings``, imported and
  driven directly with its own settings object pointed at the cohort DB;
  its LanceDB leg takes the "directory does not exist - nothing to clear"
  branch, so no live vector store is needed) resets the cohort file, then
  the REAL sync runs — and the cohort must end with a fresh pending job
  (the printed "will auto-reprocess" promise's precondition). At base the
  sync settles the file to error with no fresh job instead.

Fixture strategy mirrors tests/test_b13_lease_reindex_durability.py (real
schema via ``run_migrations``, ``SQLiteConnectionPool``, real sync entry
point) plus the scripts-module import idiom of
tests/test_b05_migrate_embeddings_safety.py.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.job_lease import ensure_jobs_schema

INGESTION_QUEUE = "ingestion"
# The cohort fixture keys on the live settings value (imported at module
# scope below); tests re-read settings.jobs_max_attempts per seed so a
# drifted default desynchronizes loudly instead of silently.
from app.config import settings as _settings


def _cap() -> int:
    return int(_settings.jobs_max_attempts)


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_cohort(db_path, *, files_status):
    """Seed the #872 cohort: old capped-failed job, LATER completed job.

    The completed job is inserted after the failed one, so it is
    unambiguously the file's LATEST ingestion job (higher id, later
    created_at). The files row lands in ``files_status`` ('indexed' for the
    pre-reset shape AC6 resets from; 'pending' for the already-reset shape
    AC5 drives directly).
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, ?, 1, ?, 'queued', 'upload')",
            ("uploads/ac872-doc.txt", "ac872-doc.txt", files_status),
        )
        file_id = int(cur.lastrowid)
        ensure_jobs_schema(conn)
        payload = json.dumps(
            {
                "file_path": "uploads/ac872-doc.txt",
                "vault_id": 1,
                "source": "upload",
                "file_id": file_id,
                "file_hash": None,
            }
        )
        # OLD job: terminally failed at the attempt cap (the stale row the
        # anti-join keys on).
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, attempts, error) "
            "VALUES (?, ?, 'failed', ?, 'attempt_cap_exceeded: PARSE_FAILED')",
            (INGESTION_QUEUE, payload, _cap()),
        )
        # LATER job for the SAME file: completed successfully.
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, attempts, "
            "result_json) VALUES (?, ?, 'completed', 1, '{}')",
            (INGESTION_QUEUE, payload),
        )
        conn.commit()
        return file_id
    finally:
        conn.close()


def _pending_job_count(conn, file_id):
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE queue = ? AND status = 'pending' "
            "AND CAST(json_extract(payload_json, '$.file_id') AS INTEGER) = ?",
            (INGESTION_QUEUE, file_id),
        ).fetchone()[0]
    )


def _assert_recovered(conn, file_id):
    """The #872 contract: fresh pending job minted, no cap-error settle."""
    pending = _pending_job_count(conn, file_id)
    assert pending == 1, (
        "a file whose LATEST ingestion job completed (older capped-failed "
        "job is history) must be treated as job-less: the sync must mint "
        f"exactly one fresh pending job, got {pending}"
    )
    row = conn.execute(
        "SELECT status, phase, error_message FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    assert row is not None, "harness: the cohort file row must survive"
    assert row["status"] != "error", (
        "the file must NOT be settled to error off the stale capped job, "
        f"got status={row['status']!r} phase={row['phase']!r} "
        f"error_message={row['error_message']!r}"
    )
    assert row["error_message"] != "lease_attempt_cap_exceeded", (
        "the stale cap settle marker must not be written for a file whose "
        f"latest job completed, got error_message={row['error_message']!r}"
    )


async def test_ac5_latest_completed_job_recovers_after_reset(tmp_path):
    """C5/AC5: the latest-job-wins recovery through the real sync.

    The cohort file (old capped-failed job + LATER completed job) is reset
    to pending/queued — the post-reset state the sync exists to recover —
    and the real ``_sync_missing_ingest_job_rows`` runs. At base the cap
    anti-join matches the stale failed row regardless of recency: no fresh
    job is minted and the file settles error/lease_attempt_cap_exceeded,
    so this check fails.
    """
    db_path = str(tmp_path / "ac5.db")
    run_migrations(db_path)
    file_id = _seed_cohort(db_path, files_status="pending")
    conn = _connect(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        created = processor._sync_missing_ingest_job_rows()  # noqa: SLF001
        del created  # the row-level assertions below are the contract
        _assert_recovered(conn, file_id)
    finally:
        pool.close_all()
        conn.close()


def _load_reset_script():
    """Import the REAL scripts/reset_embeddings.py module (b05 idiom)."""
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    spec = importlib.util.spec_from_file_location(
        "reset_embeddings_ac872_test", repo_root / "scripts" / "reset_embeddings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_ac6_reset_script_cohort_gets_fresh_pending_job(tmp_path, monkeypatch):
    """C6/AC6: the reset script's auto-reprocess promise, end to end.

    The REAL ``reset_all_embeddings`` core (its own imported settings object
    pointed at the cohort DB; the LanceDB path does not exist so its
    replacement leg is a no-op — no live vector store is involved) resets
    the cohort file from 'indexed' to pending/queued, then the REAL sync
    runs. The printed "Background processor will auto-reprocess the N reset
    file(s)" promise requires the cohort to end with a fresh pending job.
    At base the sync treats the cohort as cap-exhausted (stale anti-join)
    and settles the file to error with no fresh job, so this check fails.
    """
    data_dir = tmp_path / "ac6-data"
    data_dir.mkdir()
    db_path = str(data_dir / "app.db")
    run_migrations(db_path)
    file_id = _seed_cohort(db_path, files_status="indexed")

    script = _load_reset_script()
    # The script module carries its OWN settings instance (imported as
    # backend.app.config); point ITS data_dir at the cohort so
    # settings.sqlite_path -> <data_dir>/app.db and settings.lancedb_path
    # -> <data_dir>/lancedb (absent: the "nothing to clear" branch).
    monkeypatch.setattr(script.settings, "data_dir", data_dir)
    script.reset_all_embeddings()

    reset_conn = _connect(db_path)
    try:
        row = reset_conn.execute(
            "SELECT status, phase FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert row is not None, "harness: the cohort file row must survive"
        assert row["status"] == "pending" and row["phase"] == "queued", (
            "harness: the real reset core must reset the indexed cohort file "
            f"to pending/queued, got status={row['status']!r} "
            f"phase={row['phase']!r}"
        )
    finally:
        reset_conn.close()

    pool = SQLiteConnectionPool(db_path, max_size=2)
    conn = _connect(db_path)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        processor._sync_missing_ingest_job_rows()  # noqa: SLF001
        _assert_recovered(conn, file_id)
    finally:
        pool.close_all()
        conn.close()
