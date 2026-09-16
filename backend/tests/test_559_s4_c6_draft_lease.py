"""Issue #559 stage-4 acceptance check C6 (DISCRIMINATING): draft job claims
route through the SAME shared lease class, parameterized on ``draft_jobs``.

Stage-4 contract under test (issue #559, stage 4):

- ``DraftStore.claim_next_parse_job`` / ``claim_next_compile_job`` are served
  by the shared lease claim (single-statement) with ``worker_id`` +
  ``lease_generation`` fencing added to ``draft_jobs`` via migration — no
  parallel draft-specific claim implementation;
- the janitor reclaims a stale draft lease (``heartbeat_at`` expired) without
  a restart, bumping the generation;
- a reclaimed compile run's LATE writes are fenced: stage-progress/heartbeat
  writes and terminal status writes from the original worker are
  rejected/no-op after the reclaim and must not clobber the new claimant's row;
- unchanged semantics that must hold in the new path: two concurrent claims
  yield exactly one job; a completed/cancelled job is never overwritten by a
  late complete; ``request_job_cancel`` on a running job still settles it via
  cooperative cancel; the compile claim preserves ``started_at`` for
  recovered jobs (deadline continuation); the parse claim still orders by
  ``created_at`` then ``id`` within ``job_type``; enqueue idempotency (partial
  unique indexes) unchanged.

Store-level harness: real schema on a temp SQLite DB (``init_db`` +
``run_migrations``, the same fixture as ``tests/draft_room/
test_draft_job_processor.py``), drives DraftStore + the shared lease only.
Crashes are simulated by backdating ``heartbeat_at`` and abandoning worker
state, never by killing processes. NEW-SURFACE pins (clean TypeError/
AssertionError inside the test body at the base): the ``table=`` parameter of
``JobLease`` for the draft_jobs parameterization.

Expectation: RED at the pre-stage-4 base — ``draft_jobs`` has no
``worker_id``/``lease_generation`` columns, no draft janitor/reclaim exists,
and the claim is the legacy SELECT+UPDATE pair. GREEN post-fix.
"""

import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import mkdtemp

from app.models.database import init_db, run_migrations
from app.services.draft_store import DraftConflictError, DraftStore
from app.services.job_lease import JobLease

RECLAIM_TIMEOUT_SECONDS = 15
STALE_SECONDS = 120  # well beyond the reclaim timeout


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _make_db():
    temp = Path(mkdtemp(prefix="s4-draft-"))
    db_path = str(temp / "draft-lease.db")
    init_db(db_path)
    run_migrations(db_path)
    conn = _connect(db_path)
    conn.executescript(
        """
        INSERT OR IGNORE INTO users (id, username, hashed_password)
            VALUES (1, 's4-user', 'x');
        INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 's4-vault');
        INSERT OR IGNORE INTO drafts (id, vault_id, created_by, title, mode)
            VALUES (1, 1, 1, 's4-draft', 'rewrite');
        """
    )
    conn.commit()
    return db_path, conn


def _insert_input(conn, n):
    cur = conn.execute(
        "INSERT INTO draft_inputs (draft_id, role, authority, original_name, "
        "stored_name, extension, media_type, size_bytes, content_sha256, "
        "storage_relpath) VALUES (1, 'reference', 'unknown', ?, ?, '.txt', "
        "'text/plain', 5, ?, ?)",
        (f"in{n}.txt", f"in{n}.txt", f"sha-s4-{n}", f"inputs/in{n}.txt"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_compile_job(conn):
    cur = conn.execute(
        "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
        "max_model_calls, timeout_seconds) VALUES (1, 1, 1, 'compile', 5, 3600)"
    )
    conn.commit()
    return int(cur.lastrowid)


def _enqueue_parse(conn, input_id):
    return DraftStore(conn).enqueue_parse_job(
        draft_id=1, owner_id=1, input_id=input_id, timeout_seconds=60
    )


def _claim(store, method_name, worker_id):
    """Claim tolerantly: pass the worker identity when the lease-era signature
    accepts it, else fall back to the base (argument-less) signature."""
    method = getattr(store, method_name)
    try:
        return method(worker_id=worker_id)
    except TypeError:
        return method()


def _lease_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(draft_jobs)")}


def _require_lease_columns(conn):
    """The migration must add lease-ownership columns to draft_jobs."""
    columns = _lease_columns(conn)
    missing = {"worker_id", "lease_generation"} - columns
    assert not missing, (
        f"draft_jobs is missing lease-ownership column(s) {sorted(missing)}; "
        "the stage-4 migration must add worker_id + lease_generation fencing"
    )


def _draft_lease(conn):
    """The shared lease class parameterized on the draft_jobs table."""
    return JobLease(
        conn,
        reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS,
        table="draft_jobs",
    )


def _row(conn, job_id):
    return conn.execute(
        "SELECT id, status, worker_id, lease_generation, started_at, "
        "heartbeat_at, completed_at, result_json, active_stage, "
        "model_call_count FROM draft_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()


def _age_heartbeat(conn, job_id, seconds=STALE_SECONDS):
    conn.execute(
        "UPDATE draft_jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
        (f"-{seconds} seconds", job_id),
    )
    conn.commit()


class TestDraftLeaseOwnershipSchema(unittest.TestCase):
    # check: C6 (DISCRIMINATING) — the migration adds lease ownership.

    def setUp(self):
        self.db_path, self.conn = _make_db()
        self.addCleanup(self.conn.close)

    def test_migration_adds_worker_and_generation_columns(self):
        self.assertIn("worker_id", _lease_columns(self.conn))
        self.assertIn("lease_generation", _lease_columns(self.conn))


class TestDraftClaimsServeThroughSharedLease(unittest.TestCase):
    # check: C6 (DISCRIMINATING) — DraftStore claims are served by the shared
    # single-statement lease claim (worker_id + lease_generation stamped),
    # keeping the base claim semantics.

    def setUp(self):
        self.db_path, self.conn = _make_db()
        self.addCleanup(self.conn.close)

    def test_parse_claim_stamps_lease_ownership(self):
        _require_lease_columns(self.conn)
        job = _enqueue_parse(self.conn, _insert_input(self.conn, 1))

        claimed = _claim(DraftStore(self.conn), "claim_next_parse_job", "w-parse")

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, "running")
        row = _row(self.conn, job.id)
        self.assertEqual(row["status"], "running")
        self.assertIsNotNone(row["heartbeat_at"])
        self.assertIsNotNone(
            row["worker_id"], "the lease-era claim must stamp a worker identity"
        )
        self.assertGreaterEqual(
            int(row["lease_generation"] or 0),
            1,
            "the lease-era claim must advance the fencing generation",
        )

    def test_two_racing_parse_claims_yield_exactly_one_job(self):
        _require_lease_columns(self.conn)
        job = _enqueue_parse(self.conn, _insert_input(self.conn, 1))

        results = [None, None]
        barrier = threading.Barrier(2)

        def claim(slot):
            conn = _connect(self.db_path)
            try:
                barrier.wait(timeout=5)
                results[slot] = _claim(
                    DraftStore(conn), "claim_next_parse_job", f"racer-{slot}"
                )
            finally:
                conn.close()

        threads = [threading.Thread(target=claim, args=(s,)) for s in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        claimed = [r for r in results if r is not None]
        self.assertEqual(len(claimed), 1, f"exactly one racer may win: {results!r}")
        self.assertEqual(len([r for r in results if r is None]), 1)
        self.assertEqual(claimed[0].id, job.id)
        self.assertEqual(claimed[0].status, "running")

    def test_compile_claim_preserves_started_at_for_recovered_job(self):
        _require_lease_columns(self.conn)
        job_id = _insert_compile_job(self.conn)

        first = _claim(DraftStore(self.conn), "claim_next_compile_job", "w-first")
        self.assertIsNotNone(first)
        original = _row(self.conn, job_id)["started_at"]
        self.assertIsNotNone(original)
        # A recovered job already burned most of its deadline: pin a
        # distinctive original claim time (issue #516 DRAFT-013 semantics).
        self.conn.execute(
            "UPDATE draft_jobs SET started_at = datetime('now', '-600 seconds') "
            "WHERE id = ?",
            (job_id,),
        )
        self.conn.commit()
        distinctive = _row(self.conn, job_id)["started_at"]

        # Crash the holder, let the janitor reclaim without a restart.
        _age_heartbeat(self.conn, job_id)
        self.assertGreaterEqual(_draft_lease(self.conn).reclaim_expired(), 1)
        reclaimed = _row(self.conn, job_id)
        self.assertEqual(reclaimed["status"], "pending")
        self.assertEqual(
            reclaimed["started_at"],
            distinctive,
            "reclaim must not clear started_at (deadline continuation)",
        )

        second = _claim(DraftStore(self.conn), "claim_next_compile_job", "w-second")
        self.assertIsNotNone(second)
        self.assertEqual(
            _row(self.conn, job_id)["started_at"],
            distinctive,
            "the re-claim must COALESCE onto the original started_at, not "
            "re-grant the full timeout",
        )


class TestDraftJanitorReclaimAndFencing(unittest.TestCase):
    # check: C6 (DISCRIMINATING) — the janitor reclaims a stale draft lease
    # without a restart (generation bumped) and the ORIGINAL holder's LATE
    # stage-progress/heartbeat-style and terminal writes are fenced off.

    def setUp(self):
        self.db_path, self.conn = _make_db()
        self.addCleanup(self.conn.close)

    def _claim_compile(self, worker_id):
        job_id = _insert_compile_job(self.conn)
        job = _claim(DraftStore(self.conn), "claim_next_compile_job", worker_id)
        self.assertIsNotNone(job)
        return job.id

    def test_stale_draft_lease_reclaimed_without_restart_bumping_generation(self):
        _require_lease_columns(self.conn)
        job_id = self._claim_compile("w-stalled")
        generation_before = int(_row(self.conn, job_id)["lease_generation"] or 0)

        _age_heartbeat(self.conn, job_id)
        settled = _draft_lease(self.conn).reclaim_expired()

        self.assertGreaterEqual(settled, 1)
        row = _row(self.conn, job_id)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["worker_id"])
        self.assertGreater(
            int(row["lease_generation"] or 0),
            generation_before,
            "reclaim must bump the fencing generation",
        )
        # The reclaimed job is servable again by a new claimant, no restart.
        regrabbed = _claim(DraftStore(self.conn), "claim_next_compile_job", "w-new")
        self.assertIsNotNone(regrabbed)
        self.assertEqual(regrabbed.id, job_id)

    def test_late_writes_from_reclaimed_holder_are_fenced(self):
        _require_lease_columns(self.conn)
        job_id = self._claim_compile("w-original")
        # Normalize the holder identity the claim stamped to a known value:
        # whatever identity the claim assigns, fencing must bound writes to it.
        self.conn.execute(
            "UPDATE draft_jobs SET worker_id = 'holder-original' WHERE id = ?",
            (job_id,),
        )
        self.conn.commit()

        # The holder stalls; the janitor reclaims; a new claimant takes over.
        _age_heartbeat(self.conn, job_id)
        self.assertGreaterEqual(_draft_lease(self.conn).reclaim_expired(), 1)
        regrabbed = _claim(DraftStore(self.conn), "claim_next_compile_job", "w-new")
        self.assertIsNotNone(regrabbed)
        self.conn.execute(
            "UPDATE draft_jobs SET worker_id = 'holder-new' WHERE id = ?",
            (job_id,),
        )
        # Pin a distinctive stale heartbeat so any late mutation is visible
        # regardless of CURRENT_TIMESTAMP granularity.
        self.conn.execute(
            "UPDATE draft_jobs SET heartbeat_at = datetime('now', '-90 seconds') "
            "WHERE id = ?",
            (job_id,),
        )
        self.conn.commit()
        baseline = _row(self.conn, job_id)
        self.assertEqual(baseline["status"], "running")

        # The ORIGINAL holder wakes up and tries its stage-progress/heartbeat
        # update and its terminal writes through the shared lease API.
        stale_conn = _connect(self.db_path)
        self.addCleanup(stale_conn.close)
        stale_lease = _draft_lease(stale_conn)
        self.assertFalse(
            stale_lease.heartbeat(job_id, "holder-original"),
            "a heartbeat-style write from the reclaimed holder must be "
            "rejected",
        )
        self.assertFalse(
            stale_lease.complete(job_id, "holder-original", {"late": True}),
            "a terminal complete from the reclaimed holder must be rejected",
        )
        self.assertFalse(
            stale_lease.fail(job_id, "holder-original", "late failure"),
            "a terminal fail from the reclaimed holder must be rejected",
        )

        after = _row(self.conn, job_id)
        self.assertEqual(after["status"], "running", "row must stay running")
        self.assertEqual(
            after["worker_id"], "holder-new", "new claimant's ownership intact"
        )
        self.assertEqual(
            after["heartbeat_at"],
            baseline["heartbeat_at"],
            "late heartbeat-style write must not mutate the row",
        )
        self.assertIsNone(
            after["completed_at"], "late terminal write must not settle the row"
        )
        self.assertEqual(after["result_json"], baseline["result_json"])

        # The new claimant's writes still land: the row stays consistent for it.
        live_lease = _draft_lease(self.conn)
        self.assertTrue(live_lease.heartbeat(job_id, "holder-new"))
        self.assertTrue(live_lease.complete(job_id, "holder-new", {"ok": 1}))
        self.assertEqual(_row(self.conn, job_id)["status"], "completed")

    def test_late_complete_never_overwrites_terminal_job(self):
        _require_lease_columns(self.conn)

        # Completed shape: an idempotent duplicate complete is fenced off.
        done_id = self._claim_compile("w-done")
        self.conn.execute(
            "UPDATE draft_jobs SET worker_id = 'holder-done' WHERE id = ?",
            (done_id,),
        )
        self.conn.commit()
        lease = _draft_lease(self.conn)
        self.assertTrue(lease.complete(done_id, "holder-done", {"run": 1}))
        self.assertFalse(lease.complete(done_id, "holder-done", {"run": 2}))
        self.assertFalse(lease.fail(done_id, "holder-done", "too late"))
        row = _row(self.conn, done_id)
        self.assertEqual(row["status"], "completed")
        self.assertIn('"run": 1', row["result_json"])
        self.assertNotIn('"run": 2', row["result_json"])

        # Cancelled shape: claim -> cooperative cancel settle -> late complete.
        cancelled_id = self._claim_compile("w-cancel")
        self.conn.execute(
            "UPDATE draft_jobs SET worker_id = 'holder-cancel' WHERE id = ?",
            (cancelled_id,),
        )
        self.conn.commit()
        store = DraftStore(self.conn)
        store.request_job_cancel(draft_id=1, owner_id=1, job_id=cancelled_id)
        marked = _row(self.conn, cancelled_id)
        self.assertEqual(marked["status"], "running")
        store.set_job_status(job_id=cancelled_id, target="cancelled")
        settled = _row(self.conn, cancelled_id)
        self.assertEqual(settled["status"], "cancelled")
        self.assertIsNotNone(settled["completed_at"])

        self.assertFalse(
            lease.complete(cancelled_id, "holder-cancel", {"late": True}),
            "a late complete must never overwrite a cancelled job",
        )
        after = _row(self.conn, cancelled_id)
        self.assertEqual(after["status"], "cancelled")
        self.assertEqual(after["completed_at"], settled["completed_at"])
        self.assertEqual(after["result_json"], settled["result_json"])


class TestDraftUnchangedSemantics(unittest.TestCase):
    # check: C6 (DISCRIMINATING) — unchanged semantics that must hold in the
    # new path: cooperative cancel, parse claim ordering, enqueue idempotency.

    def setUp(self):
        self.db_path, self.conn = _make_db()
        self.addCleanup(self.conn.close)

    def test_request_job_cancel_on_running_job_settles_cooperatively(self):
        _require_lease_columns(self.conn)
        job = _enqueue_parse(self.conn, _insert_input(self.conn, 1))
        claimed = _claim(DraftStore(self.conn), "claim_next_parse_job", "w-run")
        self.assertIsNotNone(claimed)

        store = DraftStore(self.conn)
        record = store.request_job_cancel(draft_id=1, owner_id=1, job_id=job.id)

        # Cooperative, not immediate: the running job keeps running with a
        # cancel marker until the worker observes it and settles.
        self.assertEqual(record.status, "running")
        row = _row(self.conn, job.id)
        self.assertEqual(row["status"], "running")
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT cancel_requested_at FROM draft_jobs WHERE id = ?",
                (job.id,),
            ).fetchone()[0],
            "the cooperative cancel marker must be set",
        )

        # The worker's cooperative settle still lands.
        store.set_job_status(job_id=job.id, target="cancelled")
        self.assertEqual(_row(self.conn, job.id)["status"], "cancelled")

    def test_parse_claim_orders_by_created_at_then_id(self):
        _require_lease_columns(self.conn)
        first = _enqueue_parse(self.conn, _insert_input(self.conn, 1))
        second = _enqueue_parse(self.conn, _insert_input(self.conn, 2))
        third = _enqueue_parse(self.conn, _insert_input(self.conn, 3))
        # created_at ordering deliberately differs from id ordering: job 2 is
        # oldest, jobs 1 and 3 share a newer timestamp (id breaks the tie).
        self.conn.execute(
            "UPDATE draft_jobs SET created_at = datetime('now', '-60 seconds') "
            "WHERE id = ?",
            (second.id,),
        )
        self.conn.execute(
            "UPDATE draft_jobs SET created_at = datetime('now', '-10 seconds') "
            "WHERE id IN (?, ?)",
            (first.id, third.id),
        )
        self.conn.commit()

        store = DraftStore(self.conn)
        claimed = [
            _claim(store, "claim_next_parse_job", f"w-order-{n}") for n in range(3)
        ]
        self.assertEqual(
            [job.id for job in claimed],
            [second.id, first.id, third.id],
            "parse claims must order by created_at then id",
        )

    def test_enqueue_idempotency_unchanged(self):
        _require_lease_columns(self.conn)
        input_id = _insert_input(self.conn, 1)
        _enqueue_parse(self.conn, input_id)
        with self.assertRaises(DraftConflictError):
            _enqueue_parse(self.conn, input_id)

        _insert_compile_job(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_compile_job(self.conn)

        index_names = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'draft_jobs'"
            )
        }
        self.assertIn("idx_draft_jobs_one_active_parse", index_names)
        self.assertIn("idx_draft_jobs_one_active_compile", index_names)


if __name__ == "__main__":
    unittest.main()
