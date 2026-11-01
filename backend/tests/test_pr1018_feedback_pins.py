"""PR #1018 feedback-round pins (non-frozen; the swarm-pr-review
REQUEST_CHANGES round's regression floor).

Covers the review findings the round fixes:
* F-001: a reclaim-only PUT that raises reclaim above the live stall
  budget is rejected at PUT time, and the pair it used to persist cannot
  boot (the companion legs live in tests/test_559_job_lease_settings.py);
* F-002 companion: a DETERMINISTIC stall-release test that synchronizes on
  the first ``running`` observation and rejects every pre-claim
  non-running observation as release evidence (the frozen C7 oracle can
  false-pass on the seeded pre-claim row — 2/11 instrumented runs);
* B2 (out-of-band review): a stall cancel merges files that already FAILED
  earlier in the attempt into $.retry_file_ids — file1 raises, file2 hangs
  → scope [file1, file2, file3], never a false success;
* F-003: NaN/inf env values are rejected by the float range validator
  ('-nan seconds' silently disables the stall machinery);
* C-20: claim() resets the progress watermark (a re-claimed row's stall
  baseline is claim-time, not a prior attempt's stale watermark);
* F-005/C-26: the probe response oracle rejects EMPTY containers in every
  dialect and accepts the native-TEI {"embeddings": [[...]]} wrapper;
* C-51: the probe payload derives from the settings knobs (5 x 40 in);
* C-27: the legacy scalar-Ollama fallback names itself in the error text
  on the outage path too;
* C-43: the Dockerfile NLTK bake RUN has no ``|| true`` skip fallback.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, settings

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# F-003 — non-finite float env values rejected
# ---------------------------------------------------------------------------


class TestNonFiniteFloatSettingsRejected:
    def _build(self, **overrides) -> Settings:
        return Settings(**overrides)

    def test_nan_stall_timeout_rejected(self):
        with pytest.raises(ValidationError, match="finite"):
            self._build(jobs_stall_timeout_seconds=float("nan"))

    def test_inf_write_lock_timeout_rejected(self):
        with pytest.raises(ValidationError, match="finite"):
            self._build(write_lock_timeout_seconds=float("inf"))

    def test_heartbeat_nan_rejected(self):
        with pytest.raises(ValidationError, match="finite"):
            self._build(jobs_heartbeat_interval_seconds=float("nan"))

    def test_finite_stall_boundary_still_accepted(self):
        built = self._build(jobs_stall_timeout_seconds=901.5)
        assert built.jobs_stall_timeout_seconds == 901.5


# ---------------------------------------------------------------------------
# F-002 companion — deterministic stall release (no pre-claim escape)
# ---------------------------------------------------------------------------


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


async def test_stall_release_deterministic_when_synchronized_on_claim(
    tmp_path, monkeypatch
):
    """The frozen C7 oracle can observe the seeded PRE-CLAIM 'pending' row
    and count it as release evidence (2/11 instrumented false passes).
    This companion synchronizes on the first RUNNING observation before
    evaluating release, so the verdict only counts post-claim evidence."""
    import test_incident_resilience_support as sup

    db_path, conn = sup._seed(tmp_path, "pins-f002.db")
    pool = sup.SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        sup._insert_file(conn, status="indexed", name="pins-f002.txt")
        sup._insert_job(conn, {"vault_id": None}, status="pending")

        processor = sup.BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001

        async def stalled_double(existing_id, file_path, vault_id, **kw):  # noqa: ARG001
            await asyncio.Event().wait()

        monkeypatch.setattr(
            processor.processor, "process_existing_file", stalled_double
        )
        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", 2.0)
        worker = asyncio.create_task(
            processor._reindex_lease_worker_loop()  # noqa: SLF001
        )
        seen_running = False
        observed = []
        released = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 12.0
        while loop.time() < deadline:
            await asyncio.sleep(0.05)
            await processor._janitor_sweep_once()  # noqa: SLF001
            row = conn.execute(
                "SELECT status, attempts FROM jobs WHERE queue = ? "
                "ORDER BY id DESC LIMIT 1",
                (sup.REINDEX_QUEUE,),
            ).fetchone()
            if row is None:
                continue
            observed.append(row["status"])
            if row["status"] == "running":
                seen_running = True
            # Release evidence ONLY counts post-claim observations.
            if seen_running and (
                row["status"] != "running" or int(row["attempts"]) >= 2
            ):
                released = True
                break

        assert seen_running, "harness: the worker must claim the job"
        assert released, (
            "a live worker whose job makes zero progress must lose its "
            "lease inside the stall budget (post-claim evidence only); "
            f"observed statuses={observed[-15:]!r}"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()

# ---------------------------------------------------------------------------
# B2 — stall retry scope merges files that already failed this attempt
# ---------------------------------------------------------------------------


async def test_stall_cancel_merges_earlier_failures_into_retry_scope(
    tmp_path, monkeypatch
):
    """file1 raises (failed_ids=[1]), file2 hangs (stall-cancelled): the
    settle scope must be [file1, file2, file3] — a scope missing file1
    would strand its error row and report false success (out-of-band
    review B2, harness-proven)."""
    import test_incident_resilience_support as sup

    db_path, conn = sup._seed(tmp_path, "pins-b2.db")
    pool = sup.SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        ids = [
            sup._insert_file(conn, status="indexed", name=f"pins-b2-{i}.txt")
            for i in range(3)
        ]
        sup._insert_job(conn, {"vault_id": None}, status="pending")

        processor = sup.BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001

        async def raise_then_stall(existing_id, file_path, vault_id, **kw):  # noqa: ARG001
            if int(existing_id) == ids[0]:
                raise RuntimeError("pins-b2: transient embed failure")
            await asyncio.Event().wait()

        monkeypatch.setattr(
            processor.processor, "process_existing_file", raise_then_stall
        )
        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", 1.2)

        worker = asyncio.create_task(
            processor._reindex_lease_worker_loop()  # noqa: SLF001
        )
        row = await sup._drive_reindex_row(
            conn, 15.0, want_error="lease_stall_reclaimed"
        )
        assert row is not None, "harness: the stalled run must settle"
        payload = json.loads(row["payload_json"])
        assert payload.get("retry_file_ids") == ids, (
            "a stall cancel must merge files that already failed this "
            f"attempt into the retry scope, expected {ids!r}, got "
            f"{payload.get('retry_file_ids')!r}"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()


# ---------------------------------------------------------------------------
# C-20 — claim resets the progress watermark
# ---------------------------------------------------------------------------


async def test_claim_resets_stale_progress_watermark(tmp_path):
    """A re-claimed row must not carry its prior attempt's stale watermark:
    claim() sets progress_at to claim-time (the watchdog/janitor stall
    baseline is the fresh claim, never the dead attempt's watermark)."""
    import test_incident_resilience_support as sup

    db_path, conn = sup._seed(tmp_path, "pins-c20.db")
    try:
        sup._insert_job(
            conn,
            {"vault_id": None},
            status="pending",
            worker_id=None,
        )
        conn.execute(
            "UPDATE jobs SET progress_at = datetime('now', '-3600 seconds') "
            "WHERE queue = ?",
            (sup.REINDEX_QUEUE,),
        )
        conn.commit()
        before = conn.execute(
            "SELECT progress_at FROM jobs WHERE queue = ?",
            (sup.REINDEX_QUEUE,),
        ).fetchone()["progress_at"]

        lease = sup.JobLease(
            conn,
            reclaim_timeout_seconds=300.0,
            max_attempts=3,
            stall_timeout_seconds=900.0,
        )
        row = lease.claim(sup.REINDEX_QUEUE, "pins-c20-worker")
        assert row is not None, "harness: the row must be claimable"
        after = conn.execute(
            "SELECT progress_at FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()["progress_at"]
        assert after is not None, "claim must stamp a fresh watermark"
        assert after != before, (
            f"claim must reset the stale watermark ({before!r} survived)"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C-22 — external cancellation during a QUEUED parse releases the slot
# ---------------------------------------------------------------------------


def test_parse_slot_released_when_cancelled_while_queued(monkeypatch):
    """A watchdog CancelledError landing while the parse work item is still
    QUEUED drops the item — the slot must be released (out-of-band review
    C-22: pre-fix, the slot leaked until restart and every retry of the
    file was refused with ParseInFlightError). Discriminates on the QUEUED
    case: a RUNNING parse always self-releases via the thread's finally,
    frozen since #703."""
    import threading as threading_mod
    from concurrent.futures import ThreadPoolExecutor

    from app.services import document_processor as dp

    monkeypatch.setattr(settings, "document_parse_timeout", 30.0)
    test_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pins-c22")
    monkeypatch.setattr(dp, "_PARSE_EXECUTOR", test_executor)
    release_block = threading_mod.Event()

    async def scenario():
        # Occupy the single worker so the target parse stays QUEUED.
        blocker = test_executor.submit(release_block.wait, 10.0)
        try:
            task = asyncio.ensure_future(
                dp._parse_with_deadline(
                    "pins-c22-queued.pdf",
                    lambda: "parsed",
                    stage="pins",
                )
            )
            await asyncio.sleep(0.3)  # let the submission queue up
            key = dp._parse_registry_key("pins-c22-queued.pdf")
            held_while_queued = key in dp._PARSE_IN_FLIGHT
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            still_held = key in dp._PARSE_IN_FLIGHT
            return held_while_queued, still_held
        finally:
            release_block.set()
            blocker.result()

    held_queued, held_after = asyncio.run(scenario())
    assert held_queued, "harness: the slot must be reserved while queued"
    assert not held_after, (
        "a cancelled QUEUED parse must release its parse slot (the "
        "dropped work item's finally never runs) — C-22"
    )


# ---------------------------------------------------------------------------
# C-43 — the NLTK bake RUN has no skip fallback
# ---------------------------------------------------------------------------


def test_dockerfile_nltk_bake_has_no_skip_fallback():
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    backend_stage = text.split("AS backend", 1)[1]
    nltk_lines = [
        line
        for line in backend_stage.splitlines()
        if "nltk.downloader" in line or "nltk.data.find" in line
    ]
    assert nltk_lines, "harness: the NLTK bake RUN must exist"
    for line in nltk_lines:
        assert "|| true" not in line and "|| echo" not in line, (
            "the NLTK bake must not carry a skip fallback (issue #1008: "
            "nltk is a locked dependency; a missing corpus is a build "
            f"defect): {line!r}"
        )
