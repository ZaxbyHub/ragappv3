"""Issue #1020 acceptance check: the reindex progress watermark must
advance per EMBEDDING BATCH (the file named by the issue).

At base the watermark refreshes only after a whole file succeeds
(``_refresh_progress_watermark`` has exactly one call site, after
``process_existing_file`` returns — the #1010 per-file granularity), while
the stall watchdog cancels on watermark age. A healthy large document
whose cumulative embed runtime exceeds ``jobs_stall_timeout_seconds`` is
therefore cancelled mid-file, the settle requeues the same oversized file,
and the job dies at the attempts cap — the reindex can never complete on
that deployment.

Check (AC10, DISCRIMINATING): a reindex attempt whose single file embeds
in 3 serial batches (embedding_batch_size=1 forced), every batch
SUCCEEDING, each batch taking ~0.35s (file total ~1.05s) under a 0.8s
stall budget must COMPLETE — no lease-stall watchdog cancellation. At
base the watchdog cancels the first attempt inside the file (watermark
age crosses 0.8s at the ~1.0s poll) and the requeue re-enters the same
stall until the attempts cap; post-fix the per-batch refresh keeps the
watermark age under the budget and the attempt settles 'completed'.

Harness (the test_f3_stalled_reindex_lease.py / test_incident_resilience_
support idiom): real schema via ``run_migrations``, ``SQLiteConnectionPool``,
the real ``_reindex_lease_worker_loop`` (watchdog armed in-loop), the REAL
``process_existing_file`` and the REAL ``EmbeddingService.embed_batch``
batching/gather machinery — only the provider HTTP seam
(``_embed_batch_api``) is slowed per batch, and the vector-store data
plane is the sanctioned double (``_add_chunks_unlocked``). Serial batches
are forced via embedding_concurrent_batches=1 so each per-batch completion
lands at ~0.35s intervals — the exact intra-file signal the follow-up
plumbs into the watermark. The dimension probe's one-text batch returns
without the sleep so the budget measures the file, not the probe.
"""

import asyncio
import json

import test_incident_resilience_support as sup

from app.config import settings
from app.services.background_tasks import BackgroundProcessor
from app.services.embeddings import EmbeddingService

BATCH_SLEEP_S = 0.35  # per embedding batch; 3 batches -> ~1.05s file total
STALL_BUDGET_S = 0.8  # < file total, > 2x the per-batch interval
DRIVE_DEADLINE_S = 5.0  # post-fix completes in ~1.5s; base cycles sooner

SQL_DOC = """
CREATE TABLE pr1018_a (id INTEGER PRIMARY KEY, note TEXT);
CREATE TABLE pr1018_b (id INTEGER PRIMARY KEY, tag TEXT);
CREATE TABLE pr1018_c (id INTEGER PRIMARY KEY, kind TEXT);
"""


async def test_batch_progressing_large_file_completes_under_stall_budget(
    tmp_path, monkeypatch
):
    """AC10: a file that advances batch-by-batch (every embedding request
    succeeding) but whose total runtime exceeds the stall budget must
    complete without a lease-stall cancellation."""
    db_path, conn = sup._seed(tmp_path, "pr1018-followup.db")
    pool = sup.SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        doc_path = tmp_path / "pr1018-followup-doc.sql"
        doc_path.write_text(SQL_DOC, encoding="utf-8")
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, ?, 1, 'indexed', "
            "'indexed', 'upload')",
            (str(doc_path), "pr1018-followup-doc.sql"),
        )
        conn.commit()
        file_id = int(cur.lastrowid)
        sup._insert_job(conn, {"vault_id": None}, status="pending")

        # The REAL embedding service: batch splitting, the gather, and the
        # per-batch completion points are production code. Only the provider
        # HTTP seam is slowed (a public literal-IP URL keeps the constructor
        # SSRF check offline-safe; no request is ever made).
        monkeypatch.setattr(
            settings, "ollama_embedding_url", "http://8.8.8.8:80/v1/embeddings"
        )
        service = EmbeddingService()
        file_batches: list[int] = []

        async def _slow_provider_batch(batch_texts, config):  # noqa: ARG001
            if batch_texts and "dimension_probe" in str(batch_texts[0]):
                return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]
            file_batches.append(len(batch_texts))
            await asyncio.sleep(BATCH_SLEEP_S)
            return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]

        service._embed_batch_api = _slow_provider_batch  # noqa: SLF001

        store = sup._build_store(tmp_path, [])

        # Round-2 hardening: the completion path's embedding-identity
        # sidecar writes (record_embedding_metadata / mark_ready) target the
        # store's OWN metadata DB, which a fresh test sidecar lacks — no-op
        # doubles (the same data-plane discipline as _build_store's seams)
        # so a post-fix completed attempt settles 'completed' instead of
        # failing on an out-of-scope sidecar.
        async def _noop(*args, **kwargs):  # noqa: ARG001
            return None

        store.record_embedding_metadata = _noop  # noqa: SLF001
        store.mark_ready = _noop  # noqa: SLF001
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        # b07 injection idiom: _reindex_embed_all reads the wrapped
        # DocumentProcessor's services.
        processor.processor.embedding_service = service
        processor.processor.vector_store = store
        await processor._run_jobs_migration()  # noqa: SLF001

        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", STALL_BUDGET_S)
        # Two attempts cap the BASE stall-cycle at one requeue (~3s wall);
        # post-fix the first attempt completes (~1.2s) regardless of cap.
        monkeypatch.setattr(settings, "jobs_max_attempts", 2)
        monkeypatch.setattr(settings, "embedding_batch_size", 1)
        monkeypatch.setattr(settings, "embedding_concurrent_batches", 1)
        monkeypatch.setattr(settings, "embedding_global_concurrent_batches", 1)

        worker = asyncio.create_task(processor._reindex_lease_worker_loop())  # noqa: SLF001

        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRIVE_DEADLINE_S
        final_row = None
        while loop.time() < deadline:
            await asyncio.sleep(0.2)
            await processor._janitor_sweep_once()  # noqa: SLF001
            final_row = conn.execute(
                "SELECT status, attempts, error FROM jobs "
                "WHERE queue = ? ORDER BY id DESC LIMIT 1",
                (sup.REINDEX_QUEUE,),
            ).fetchone()
            if final_row is not None and final_row["status"] in (
                "completed",
                "failed",
                "cancelled",
            ):
                break

        assert final_row is not None, "harness: the reindex job row must exist"
        assert len(file_batches) >= 3, (
            "harness: the file must actually embed in >=3 batches (one per"
            f" chunk, batch_size=1) for the per-batch scenario to be under"
            f" test, batch sizes seen={file_batches!r}"
        )
        assert final_row["status"] == "completed", (
            "a reindex attempt whose file advances through embedding in"
            f" {len(file_batches)} batches, every batch succeeding, must"
            " complete without watchdog cancellation — got status="
            f"{final_row['status']!r} error={final_row['error']!r}"
            f" attempts={final_row['attempts']}"
            " (the watermark refreshes only after the whole file at base, so"
            " the healthy file is stall-cancelled mid-embed and the requeue"
            " re-enters the same stall until the cap — issue #1020)"
        )
        assert "lease_stall_reclaimed" not in (final_row["error"] or ""), (
            "the stall reason must never appear for a batch-progressing"
            f" healthy file, got error={final_row['error']!r} (issue #1020)"
        )
        frow = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert frow["status"] == "indexed", (
            "harness: the fully-embedded file must settle indexed, got"
            f" {frow['status']!r}"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()


# ---------------------------------------------------------------------------
# R1 (round-2 amendment, critic F1) — mid-batch lease-fence loss
# ---------------------------------------------------------------------------

_R1A_DOC = """
CREATE TABLE r1a_one (id INTEGER PRIMARY KEY, note TEXT);
CREATE TABLE r1a_two (id INTEGER PRIMARY KEY, tag TEXT);
CREATE TABLE r1a_three (id INTEGER PRIMARY KEY, kind TEXT);
"""
_R1B_DOC = """
CREATE TABLE r1b_one (id INTEGER PRIMARY KEY, note TEXT);
CREATE TABLE r1b_two (id INTEGER PRIMARY KEY, tag TEXT);
"""


async def _drive_fence_loss_scenario(tmp_path, monkeypatch, *, fail_batch_two):
    """One direct ``_reindex_embed_all`` run over two files where the SECOND
    embedding batch of file A loses the lease fence.

    Planned contract (round-2 critic F1, the docstring the fix must
    satisfy): the per-batch progress callback stamps ``jobs.progress_at``
    fenced by id+worker+status; a zero-row fenced stamp (lost fence) makes
    the callback RECORD the loss and stop stamping; after the embed gather
    returns, a guard BEFORE any vector/atom write raises; the per-file
    handlers convert that to the lease-stop path (_LeaseProgressLost — stop,
    no failed_ids accumulation, no successor scope mutation); no later file
    is started, and no provider batch beyond the loss-triggering one runs.

    Simulation seam (round-3, critic R2-2): a REAL 'running' reindex jobs
    row is seeded (the harness worker owns it), and its actual id backs
    BOTH ``_active_progress`` AND the ``_reindex_embed_all`` call, so
    pre-arm stamps hit a real row. The fence is armed inside the provider
    stub as batch 2 of file A completes (or fails); the reindex's watermark
    stamp seam (``_stamp_progress_watermark``) is wrapped so every post-arm
    refresh reads as a zero-row fenced write (returns False). At BASE there
    is no per-batch refresh, so the arm is only consumed by the post-FILE
    refresh — after batch 3 (and, when fail_batch_two, the per-file failure
    ledger and the vector write) already ran — which is exactly the defect
    these checks pin.

    Returns a dict of observables for the caller's assertions.
    """
    from app.services.embeddings import EmbeddingError
    from app.services.job_lease import ensure_jobs_schema

    db_path, conn = sup._seed(tmp_path, "pr1018-r1.db")
    pool = sup.SQLiteConnectionPool(db_path, max_size=4)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'R1')"
        )
        doc_a = tmp_path / "pr1018-r1-a.sql"
        doc_a.write_text(_R1A_DOC, encoding="utf-8")
        doc_b = tmp_path / "pr1018-r1-b.sql"
        doc_b.write_text(_R1B_DOC, encoding="utf-8")
        conn.executemany(
            "INSERT INTO files (vault_id, file_path, file_name, file_size,"
            " status, phase, source) VALUES (1, ?, ?, 1, 'indexed',"
            " 'indexed', 'upload')",
            [
                (str(doc_a), "pr1018-r1-a.sql"),
                (str(doc_b), "pr1018-r1-b.sql"),
            ],
        )
        # The real claimed row: pre-arm stamps hit it (id+worker+status).
        ensure_jobs_schema(conn)
        worker_id = "r1-fence-worker"
        cur = conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id,"
            " attempts) VALUES ('reindex', ?, 'running', ?, 1)",
            (json.dumps({"vault_id": 1}), worker_id),
        )
        job_id = int(cur.lastrowid)
        conn.commit()

        monkeypatch.setattr(
            settings, "ollama_embedding_url", "http://8.8.8.8:80/v1/embeddings"
        )
        service = EmbeddingService()
        fence: dict = {"lost": False}
        a_batches: list[int] = []
        provider_failed = {"yes": False}
        b_embedded = {"seen": False}

        async def _fenced_provider_batch(batch_texts, config):  # noqa: ARG001
            if batch_texts and "dimension_probe" in str(batch_texts[0]):
                return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]
            if batch_texts and "r1a" in str(batch_texts[0]):
                a_batches.append(len(a_batches) + 1)
                if a_batches[-1] == 2:
                    # The SECOND batch's watermark stamp is a lost fence.
                    fence["lost"] = True
                    if fail_batch_two:
                        provider_failed["yes"] = True
                        raise EmbeddingError("provider 503 (r1 fence-loss leg)")
                    return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]
                return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]
            b_embedded["seen"] = True
            return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]

        service._embed_batch_api = _fenced_provider_batch  # noqa: SLF001

        written: list = []
        store = sup._build_store(tmp_path, written)

        async def _noop(*args, **kwargs):  # noqa: ARG001
            return None

        store.record_embedding_metadata = _noop  # noqa: SLF001
        store.mark_ready = _noop  # noqa: SLF001

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        processor.processor.embedding_service = service
        processor.processor.vector_store = store
        # Arm the per-attempt watermark context on the REAL claimed row
        # exactly as _run_reindex_job_row does.
        processor._active_progress = (job_id, worker_id)  # noqa: SLF001

        original_stamp = processor._stamp_progress_watermark  # noqa: SLF001

        async def _fenced_stamp():
            if fence["lost"]:
                # The simulated zero-row fenced watermark write (lost fence).
                return False
            return await original_stamp()

        processor._stamp_progress_watermark = _fenced_stamp  # noqa: SLF001

        monkeypatch.setattr(settings, "embedding_batch_size", 1)
        monkeypatch.setattr(settings, "embedding_concurrent_batches", 1)
        monkeypatch.setattr(settings, "embedding_global_concurrent_batches", 1)

        status, result, exc = await processor._reindex_embed_all(  # noqa: SLF001
            job_id, 1
        )
        file_rows = conn.execute(
            "SELECT id, status FROM files ORDER BY id"
        ).fetchall()
        ledger_rows = conn.execute("SELECT COUNT(*) FROM failed_chunks"
                                   ).fetchone()[0]
        return {
            "status": status,
            "result": result,
            "exc": exc,
            "fence_lost": fence["lost"],
            "a_batches": a_batches,
            "provider_failed": provider_failed["yes"],
            "b_embedded": b_embedded["seen"],
            "written": written,
            "file_rows": [(int(r["id"]), r["status"]) for r in file_rows],
            "ledger_rows": int(ledger_rows),
        }
    finally:
        pool.close_all()
        conn.close()


def _selectable(file_rows):
    return all(status in ("indexed", "partial", "error") for _id, status in file_rows)


async def test_batch_fence_loss_stops_attempt_under_provider_success(
    tmp_path, monkeypatch
):
    """R1(a) (critic F1, round-3 R2-2): a mid-file lease-fence loss observed
    at the second batch's watermark stamp must stop the attempt at batch
    granularity — the provider runs exactly batches [1, 2], no vector
    write, no later file, both files left selectable."""
    from app.services.background_tasks import _LeaseProgressLost  # noqa: SLF001

    observed = await _drive_fence_loss_scenario(
        tmp_path, monkeypatch, fail_batch_two=False
    )
    exc = observed["exc"]
    assert observed["fence_lost"] is True, (
        "harness: the fence must actually be armed by the second batch"
        " (fence_lost=False — the provider choreography never fired)"
    )
    assert isinstance(exc, _LeaseProgressLost) or (
        "lost its lease fence" in str(exc or "")
    ), (
        "the attempt must end on the lease-stop path (_LeaseProgressLost"
        f" semantics), got exc={exc!r}"
    )
    assert observed["a_batches"] == [1, 2], (
        "once the fence is lost at the second batch's watermark stamp, the"
        " provider must run EXACTLY batches [1, 2] — batch 3 must never be"
        f" dispatched; provider ran {observed['a_batches']!r} (at base"
        " there is no per-batch refresh at all, so the loss is only"
        " noticed after the whole file — the mechanism this check"
        " demands is absent; issue #1020 round-2 critic F1 / round-3"
        " R2-2)"
    )
    assert observed["written"] == [], (
        "the loss-stop guard must fire BEFORE any vector/atom write —"
        f" {len(observed['written'])} record(s) were written for a"
        " fence-lost attempt"
    )
    assert not observed["b_embedded"], (
        "a fence-lost attempt must not proceed to later files (file B was"
        " embedded)"
    )
    assert _selectable(observed["file_rows"]), (
        "a fence-lost attempt must leave BOTH files in a selectable state"
        f" (indexed/partial) or a truthful error, never 'processing' —"
        f" got {observed['file_rows']!r}"
    )


async def test_batch_fence_loss_with_failing_provider_batch(
    tmp_path, monkeypatch
):
    """R1(b) (critic F1, round-3 R2-2): when the loss-triggering batch ALSO
    fails at the provider, the loss-stop must take precedence over
    per-file failure handling — the injected provider failure demonstrably
    executed, no failed-chunk ledger write, no vector write, no further
    batch, no later file, both files selectable."""
    from app.services.background_tasks import _LeaseProgressLost  # noqa: SLF001

    observed = await _drive_fence_loss_scenario(
        tmp_path, monkeypatch, fail_batch_two=True
    )
    exc = observed["exc"]
    assert observed["fence_lost"] is True, (
        "harness: the fence must actually be armed by the second batch"
        " (fence_lost=False — the provider choreography never fired)"
    )
    assert observed["provider_failed"] is True, (
        "harness: the injected provider failure must demonstrably execute"
        " on the loss-triggering batch (provider_failed=False — the"
        " failure never ran, so the precedence below would be vacuous)"
    )
    assert isinstance(exc, _LeaseProgressLost) or (
        "lost its lease fence" in str(exc or "")
    ), (
        "the attempt must end on the lease-stop path (_LeaseProgressLost"
        f" semantics) even when the loss-triggering batch also failed at"
        f" the provider, got exc={exc!r}"
    )
    assert observed["a_batches"] == [1, 2], (
        "the fence loss must stop the remaining batches even though the"
        f" loss-triggering batch also failed — provider ran"
        f" {observed['a_batches']!r}, expected exactly [1, 2]"
    )
    assert observed["ledger_rows"] == 0, (
        "the loss-stop must take precedence over per-file failure"
        " handling: no failed_chunks detail row may be accumulated for a"
        f" fence-lost attempt ({observed['ledger_rows']} row(s) written)"
    )
    assert observed["written"] == [], (
        "the loss-stop guard must fire BEFORE any vector/atom write even"
        f" on the failing-batch path — {len(observed['written'])}"
        " record(s) were written"
    )
    assert not observed["b_embedded"], (
        "a fence-lost attempt must not proceed to later files (file B was"
        " embedded)"
    )
    assert _selectable(observed["file_rows"]), (
        "a fence-lost attempt must leave BOTH files in a selectable state"
        f" (indexed/partial) or a truthful error, never 'processing' —"
        f" got {observed['file_rows']!r}"
    )


_R1A4_DOC = """
CREATE TABLE r1c_one (id INTEGER PRIMARY KEY, note TEXT);
CREATE TABLE r1c_two (id INTEGER PRIMARY KEY, tag TEXT);
CREATE TABLE r1c_three (id INTEGER PRIMARY KEY, kind TEXT);
CREATE TABLE r1c_four (id INTEGER PRIMARY KEY, label TEXT);
"""


async def test_batch_fence_loss_concurrency_distinguishes_running_from_queued(
    tmp_path, monkeypatch
):
    """R1(c) (round-3, critic R2-2g/R2-9): with embedding_concurrent_batches=2
    the batch ALREADY INSIDE the provider when the fence is lost completes,
    while semaphore-QUEUED batches are never dispatched.

    Choreography (Events and bounded sleeps only on the slow in-provider
    batch): file A has 4 chunks; batches 1 and 2 are admitted by the
    semaphore; batch 1 parks 0.15s inside the provider (already running),
    batch 2 completes immediately and arms the fence; batches 3 and 4 are
    still semaphore-QUEUED at that moment. At base the freed slot lets
    batch 3 (then 4) dispatch — the dispatch counters prove queued batches
    ran; post-fix the recorded loss must keep them from ever dispatching,
    while batch 1 (already inside) completes."""
    from app.services.background_tasks import _LeaseProgressLost  # noqa: SLF001
    from app.services.job_lease import ensure_jobs_schema

    db_path, conn = sup._seed(tmp_path, "pr1018-r1c.db")
    pool = sup.SQLiteConnectionPool(db_path, max_size=4)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'R1')"
        )
        doc_a = tmp_path / "pr1018-r1c-a.sql"
        doc_a.write_text(_R1A4_DOC, encoding="utf-8")
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size,"
            " status, phase, source) VALUES (1, ?, ?, 1, 'indexed',"
            " 'indexed', 'upload')",
            (str(doc_a), "pr1018-r1c-a.sql"),
        )
        ensure_jobs_schema(conn)
        worker_id = "r1-fence-worker-c"
        cur = conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id,"
            " attempts) VALUES ('reindex', ?, 'running', ?, 1)",
            (json.dumps({"vault_id": 1}), worker_id),
        )
        job_id = int(cur.lastrowid)
        conn.commit()

        monkeypatch.setattr(
            settings, "ollama_embedding_url", "http://8.8.8.8:80/v1/embeddings"
        )
        service = EmbeddingService()
        fence: dict = {"lost": False}
        entered: list[int] = []
        completed: set = set()

        async def _fenced_provider_batch(batch_texts, config):  # noqa: ARG001
            if batch_texts and "dimension_probe" in str(batch_texts[0]):
                return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]
            entered.append(len(entered) + 1)
            which = entered[-1]
            try:
                if which == 1:
                    # Already inside the provider when the loss fires.
                    await asyncio.sleep(0.15)
                elif which == 2:
                    # Completes immediately and arms the fence.
                    fence["lost"] = True
                return [[0.1, 0.2, 0.3, 0.4] for _ in batch_texts]
            finally:
                completed.add(which)

        service._embed_batch_api = _fenced_provider_batch  # noqa: SLF001

        written: list = []
        store = sup._build_store(tmp_path, written)

        async def _noop(*args, **kwargs):  # noqa: ARG001
            return None

        store.record_embedding_metadata = _noop  # noqa: SLF001
        store.mark_ready = _noop  # noqa: SLF001

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        processor.processor.embedding_service = service
        processor.processor.vector_store = store
        processor._active_progress = (job_id, worker_id)  # noqa: SLF001

        original_stamp = processor._stamp_progress_watermark  # noqa: SLF001

        async def _fenced_stamp():
            if fence["lost"]:
                return False
            return await original_stamp()

        processor._stamp_progress_watermark = _fenced_stamp  # noqa: SLF001

        monkeypatch.setattr(settings, "embedding_batch_size", 1)
        monkeypatch.setattr(settings, "embedding_concurrent_batches", 2)
        monkeypatch.setattr(settings, "embedding_global_concurrent_batches", 2)

        status, result, exc = await processor._reindex_embed_all(  # noqa: SLF001
            job_id, 1
        )
        file_rows = conn.execute(
            "SELECT id, status FROM files ORDER BY id"
        ).fetchall()

        assert fence["lost"] is True, (
            "harness: the fence must be armed by the second batch"
        )
        assert isinstance(exc, _LeaseProgressLost) or (
            "lost its lease fence" in str(exc or "")
        ), (
            "the attempt must end on the lease-stop path, got exc={exc!r}"
        )
        assert 1 in completed, (
            "harness: the batch already inside the provider when the loss"
            " fired must COMPLETE (batch 1 never finished)"
        )
        assert entered == [1, 2], (
            "with concurrency=2, the semaphore-QUEUED batches (3, 4) must"
            " never be dispatched after the fence is lost — the provider"
            f" dispatched {entered!r}; at base the freed slot lets every"
            " queued batch run (issue #1020 round-3 R2-2g/R2-9 — running"
            " and queued batches are distinguishable)"
        )
        assert written == [], (
            "the loss-stop guard must fire BEFORE any vector/atom write —"
            f" {len(written)} record(s) were written"
        )
        assert _selectable(
            [(int(r["id"]), r["status"]) for r in file_rows]
        ), (
            "a fence-lost attempt must leave the file in a selectable"
            f" state, got {[(int(r['id']), r['status']) for r in file_rows]!r}"
        )
    finally:
        pool.close_all()
        conn.close()
