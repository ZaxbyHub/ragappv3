"""Tests for WikiCompileProcessor and the new WikiCompiler job methods."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _make_env():
    from app.models.database import run_migrations
    from app.services.wiki_compiler import WikiCompiler
    from app.services.wiki_store import WikiStore

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, file_size, status) "
        "VALUES (1, 1, '/tmp/test.txt', 'test.txt', 100, 'indexed')"
    )
    conn.commit()
    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)
    return conn, store, compiler


AFOMIS_ANSWER = (
    "AFOMIS stands for Air Force Operational Medicine Information Systems. "
    "Justice Sakyi is the AFOMIS Chief and Major Justin Woods is his deputy."
)

# Two separate role-claim sentences for per-claim citation tests
TWO_CLAIM_ANSWER = (
    "Justice Sakyi is the AFOMIS Chief. "
    "AFOMIS stands for Air Force Operational Medicine. "
    "Major Justin Woods is the AFOMIS Director."
)


# ---------------------------------------------------------------------------
# WikiCompiler.compile_query_job
# ---------------------------------------------------------------------------

class TestCompileQueryJob(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_returns_skipped_when_no_answer(self):
        result = self.compiler.compile_query_job(vault_id=1, input_json={})
        self.assertTrue(result.get("skipped"))

    def test_returns_skipped_when_no_extractable_entities(self):
        result = self.compiler.compile_query_job(
            vault_id=1,
            input_json={"assistant_answer": "The weather is fine today."},
        )
        self.assertTrue(result.get("skipped"))

    def test_creates_entities_from_answer(self):
        result = self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": AFOMIS_ANSWER,
                "wiki_refs": [{"wiki_label": "W1"}],
                "doc_sources": [],
                "memories": [],
            },
        )
        self.assertFalse(result.get("skipped"))
        entity_names = {e["name"] for e in result["entities"]}
        self.assertIn("AFOMIS", entity_names)

    def test_creates_active_claims_with_per_claim_citations(self):
        # Extract the role-claim sentences from TWO_CLAIM_ANSWER so we can key per_claim_sources
        from app.services.wiki_compiler import extract_entities_from_text
        ext = extract_entities_from_text(TWO_CLAIM_ANSWER)
        self.assertGreaterEqual(len(ext.role_claims), 2, "Need at least 2 role claims for this test")
        first_sentence = ext.role_claims[0]["sentence"]
        per_claim_sources = {
            first_sentence: [{"source_kind": "document", "source_label": "S1", "file_id": 1}]
        }
        result = self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": TWO_CLAIM_ANSWER,
                "per_claim_sources": per_claim_sources,
            },
        )
        statuses = {c["status"] for c in result["claims"]}
        self.assertIn("active", statuses)
        self.assertIn("unverified", statuses)

    def test_creates_unverified_claims_without_citations(self):
        result = self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": AFOMIS_ANSWER,
                "wiki_refs": [],
                "doc_sources": [],
                "memories": [],
            },
        )
        statuses = {c["status"] for c in result["claims"]}
        self.assertIn("unverified", statuses)

    def test_creates_lint_finding_for_unverified_claim(self):
        self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": AFOMIS_ANSWER,
                "wiki_refs": [],
                "doc_sources": [],
                "memories": [],
            },
        )
        rows = self.conn.execute(
            "SELECT finding_type FROM wiki_lint_findings WHERE vault_id = 1"
        ).fetchall()
        finding_types = {dict(r)["finding_type"] for r in rows}
        self.assertIn("unsupported_claim", finding_types)

    def test_no_lint_finding_when_all_claims_cited(self):
        from app.services.wiki_compiler import extract_entities_from_text
        ext = extract_entities_from_text(TWO_CLAIM_ANSWER)
        per_claim_sources = {
            rc["sentence"]: [{"source_kind": "document", "source_label": "S1", "file_id": 1}]
            for rc in ext.role_claims
        }
        self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": TWO_CLAIM_ANSWER,
                "per_claim_sources": per_claim_sources,
            },
        )
        rows = self.conn.execute(
            "SELECT finding_type FROM wiki_lint_findings WHERE vault_id = 1"
        ).fetchall()
        finding_types = {dict(r)["finding_type"] for r in rows}
        self.assertNotIn("unsupported_claim", finding_types)


# ---------------------------------------------------------------------------
# WikiCompiler.compile_ingest_job
# ---------------------------------------------------------------------------

class TestCompileIngestJob(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()
        # Write a temporary text file with extractable content
        self._tmpfile = tempfile.NamedTemporaryFile(
            suffix=".txt", mode="w", delete=False, encoding="utf-8"
        )
        self._tmpfile.write(AFOMIS_ANSWER)
        self._tmpfile.close()
        # Update file record to point to our temp file
        self.conn.execute(
            "UPDATE files SET file_path = ?, file_name = ? WHERE id = 1",
            (self._tmpfile.name, "afomis.txt"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import os
        try:
            os.unlink(self._tmpfile.name)
        except OSError:
            pass

    def test_returns_skipped_when_no_file_id(self):
        result = self.compiler.compile_ingest_job(vault_id=1, input_json={})
        self.assertTrue(result.get("skipped"))

    def test_raises_when_file_not_in_vault(self):
        with self.assertRaises(ValueError):
            self.compiler.compile_ingest_job(
                vault_id=999, input_json={"file_id": 1}
            )

    def test_creates_page_for_document(self):
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )
        self.assertIsNotNone(result.get("page"))
        self.assertIn("document", result["page"]["slug"])

    def test_extracts_entities_from_file(self):
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )
        entity_names = {e["name"] for e in result["entities"]}
        self.assertIn("AFOMIS", entity_names)

    def test_creates_claims_with_document_provenance(self):
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )
        self.assertGreater(len(result["claims"]), 0)
        claim_ids = [c["id"] for c in result["claims"]]
        for cid in claim_ids:
            sources = self.conn.execute(
                "SELECT source_kind FROM wiki_claim_sources WHERE claim_id = ?", (cid,)
            ).fetchall()
            source_kinds = {dict(s)["source_kind"] for s in sources}
            self.assertIn("document", source_kinds)
            claim_row = self.conn.execute(
                "SELECT status FROM wiki_claims WHERE id = ?", (cid,)
            ).fetchone()
            self.assertEqual(dict(claim_row)["status"], "active")

    def test_uses_provided_text_over_file_read(self):
        result = self.compiler.compile_ingest_job(
            vault_id=1,
            input_json={
                "file_id": 1,
                "text": AFOMIS_ANSWER,
            },
        )
        self.assertFalse(result.get("skipped"))

    def test_re_parses_from_file_path_when_no_text_in_input_json(self):
        # setUp points file_path to a .txt file with AFOMIS_ANSWER; the compiler
        # must re-parse it rather than silently returning skipped.
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1}  # no "text" key
        )
        self.assertFalse(result.get("skipped"), "Should re-parse .txt file_path, not skip")
        self.assertGreater(len(result["claims"]), 0)

    def test_does_not_open_raw_file_bytes(self):
        # /dev/zero is not a regular file and has no .txt extension; the compiler
        # must skip cleanly without raising or hanging.
        self.conn.execute(
            "UPDATE files SET file_path = '/dev/zero' WHERE id = 1"
        )
        self.conn.commit()
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1}  # no text → skip
        )
        self.assertTrue(result.get("skipped"))

    def test_creates_page_even_when_extraction_is_empty(self):
        # Prose with no acronyms and no ALL-CAPS-org role claims must still
        # produce a wiki page so the wiki reflects every ingested document.
        plain_text = (
            "The product team shipped a release on Tuesday. "
            "Feedback was gathered from beta users and incorporated."
        )
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": plain_text}
        )
        self.assertFalse(
            result.get("skipped"),
            "Ingest must produce a page even when deterministic extraction is empty",
        )
        self.assertIsNotNone(result.get("page"))
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["entities"], [])
        # The page row must actually exist in the DB.
        row = self.conn.execute(
            "SELECT id, page_type, status FROM wiki_pages WHERE id = ?",
            (result["page"]["id"],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(dict(row)["status"], "needs_review")


# ---------------------------------------------------------------------------
# WikiStore concurrent claim safety
# ---------------------------------------------------------------------------

class TestClaimNextPendingJobAtomicity(unittest.TestCase):

    def _make_store_with_conn(self):
        from app.models.database import run_migrations
        from app.services.wiki_store import WikiStore

        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "test.db")
        run_migrations(db_path)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
        conn.commit()
        return WikiStore(conn), conn, db_path

    def test_claim_returns_none_when_queue_empty(self):
        store, conn, _ = self._make_store_with_conn()
        job = store.claim_next_pending_job()
        self.assertIsNone(job)
        conn.close()

    def test_claim_marks_job_running(self):
        store, conn, _ = self._make_store_with_conn()
        store.create_job(vault_id=1, trigger_type="manual")
        job = store.claim_next_pending_job()
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "running")
        conn.close()

    def test_second_claim_returns_none_when_only_one_job(self):
        store, conn, _ = self._make_store_with_conn()
        store.create_job(vault_id=1, trigger_type="manual")
        job1 = store.claim_next_pending_job()
        job2 = store.claim_next_pending_job()
        self.assertIsNotNone(job1)
        self.assertIsNone(job2)
        conn.close()

    def test_reset_running_jobs_reclaims_orphans(self):
        store, conn, _ = self._make_store_with_conn()
        store.create_job(vault_id=1, trigger_type="manual")
        # Simulate orphan: mark as running without going through processor
        conn.execute(
            "UPDATE wiki_compile_jobs SET status = 'running'"
        )
        conn.commit()
        n = store.reset_running_jobs()
        self.assertEqual(n, 1)
        row = conn.execute("SELECT status FROM wiki_compile_jobs").fetchone()
        self.assertEqual(dict(row)["status"], "pending")
        conn.close()


# ---------------------------------------------------------------------------
# WikiStore stale marking
# ---------------------------------------------------------------------------

class TestMarkClaimsStale(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def _make_claim_with_file_source(self, claim_id_label="test"):
        claim = self.store.create_claim(
            vault_id=1,
            claim_text=f"Test claim {claim_id_label}",
            source_type="document",
        )
        self.store.attach_source(
            claim_id=claim.id,
            source_kind="document",
            file_id=1,
            source_label="file:1",
            confidence=0.9,
        )
        self.conn.commit()
        return claim

    def test_sole_source_claim_becomes_stale(self):
        claim = self._make_claim_with_file_source()
        result = self.store.mark_claims_stale_by_file(file_id=1, vault_id=1)
        self.assertGreaterEqual(result["stale"], 1)
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertEqual(dict(row)["status"], "superseded")

    def test_multi_source_claim_not_stale(self):
        claim = self._make_claim_with_file_source()
        # Add a second source (memory)
        self.store.attach_source(
            claim_id=claim.id,
            source_kind="memory",
            memory_id=999,
            source_label="memory:999",
            confidence=0.5,
        )
        self.conn.commit()
        result = self.store.mark_claims_stale_by_file(file_id=1, vault_id=1)
        self.assertGreaterEqual(result["weak_provenance"], 1)
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertNotEqual(dict(row)["status"], "superseded")

    def test_vault_scope_respected(self):
        claim = self._make_claim_with_file_source()
        result = self.store.mark_claims_stale_by_file(file_id=1, vault_id=999)
        # Wrong vault — no claims should be stale
        self.assertEqual(result["stale"], 0)
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertNotEqual(dict(row)["status"], "superseded")

    # ------------------------------------------------------------------
    # mark_claims_stale_by_memory — mirrors the file variant above.
    # Sole-source memory → 'superseded' + 'stale' lint finding; multi-source
    # (memory + file) → only 'weak_provenance' finding, claim stays active.
    # ------------------------------------------------------------------

    def _make_claim_with_memory_source(self, claim_id_label="test", memory_id=42):
        claim = self.store.create_claim(
            vault_id=1,
            claim_text=f"Memory claim {claim_id_label}",
            source_type="memory",
        )
        self.store.attach_source(
            claim_id=claim.id,
            source_kind="memory",
            memory_id=memory_id,
            source_label=f"memory:{memory_id}",
            confidence=0.9,
        )
        self.conn.commit()
        return claim

    def test_sole_memory_source_claim_becomes_stale(self):
        claim = self._make_claim_with_memory_source(memory_id=42)
        result = self.store.mark_claims_stale_by_memory(memory_id=42, vault_id=1)
        self.assertEqual(result["stale"], 1)
        self.assertEqual(result["weak_provenance"], 0)
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertEqual(dict(row)["status"], "superseded")
        # Lint finding references the memory id, not a file id.
        finding = self.conn.execute(
            "SELECT finding_type, title, severity, related_claim_ids_json "
            "FROM wiki_lint_findings WHERE vault_id = ? ORDER BY id DESC LIMIT 1",
            (1,),
        ).fetchone()
        self.assertEqual(dict(finding)["finding_type"], "stale")
        self.assertEqual(dict(finding)["severity"], "medium")
        self.assertIn("memory_id=42", dict(finding)["title"])

    def test_memory_source_with_other_source_not_stale(self):
        claim = self._make_claim_with_memory_source(memory_id=42)
        # Add a file source so the claim has a second provenance path.
        self.store.attach_source(
            claim_id=claim.id,
            source_kind="document",
            file_id=1,
            source_label="file:1",
            confidence=0.5,
        )
        self.conn.commit()
        result = self.store.mark_claims_stale_by_memory(memory_id=42, vault_id=1)
        self.assertEqual(result["stale"], 0)
        self.assertEqual(result["weak_provenance"], 1)
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertNotEqual(dict(row)["status"], "superseded")
        finding = self.conn.execute(
            "SELECT finding_type, title, severity FROM wiki_lint_findings "
            "WHERE vault_id = ? ORDER BY id DESC LIMIT 1", (1,),
        ).fetchone()
        self.assertEqual(dict(finding)["finding_type"], "weak_provenance")
        self.assertEqual(dict(finding)["severity"], "low")
        self.assertIn("memory_id=42", dict(finding)["title"])

    def test_memory_vault_scope_respected(self):
        claim = self._make_claim_with_memory_source(memory_id=42)
        result = self.store.mark_claims_stale_by_memory(memory_id=42, vault_id=999)
        # Wrong vault — no claims should be stale or weak.
        self.assertEqual(result["stale"], 0)
        self.assertEqual(result["weak_provenance"], 0)
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertNotEqual(dict(row)["status"], "superseded")
        # And no lint finding should have been written either.
        finding_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings WHERE vault_id = ?", (999,)
        ).fetchone()[0]
        self.assertEqual(finding_count, 0)

    def test_memory_unknown_id_is_noop(self):
        # No claim has memory_id=7777; running the method should not raise
        # and should report zero stale/weak findings.
        result = self.store.mark_claims_stale_by_memory(memory_id=7777, vault_id=1)
        self.assertEqual(result, {"stale": 0, "weak_provenance": 0})


# ---------------------------------------------------------------------------
# WikiCompileProcessor lifecycle
# ---------------------------------------------------------------------------

class TestWikiCompileProcessorLifecycle(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        from app.models.database import SQLiteConnectionPool, run_migrations

        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "test.db")
        run_migrations(db_path)
        self.pool = SQLiteConnectionPool(db_path, max_size=3)
        with self.pool.connection() as conn:
            conn.row_factory = None
            conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
            conn.commit()

    async def asyncTearDown(self):
        self.pool.close_all()

    async def test_start_and_stop(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor

        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        self.assertTrue(proc._running)
        await proc.stop()
        self.assertFalse(proc._running)

    async def test_second_start_is_noop(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor

        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        task_before = proc._task
        await proc.start()  # must not create a second task
        self.assertIs(proc._task, task_before)
        await proc.stop()

    async def test_processes_manual_job(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        # Give the processor a moment to claim and process the job
        await asyncio.sleep(0.5)
        await proc.stop()

        with self.pool.connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT status FROM wiki_compile_jobs").fetchone()
        self.assertIn(dict(row)["status"], ("completed", "failed"))

    async def test_orphan_recovery_on_start(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        # Plant an orphaned running job
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO wiki_compile_jobs (vault_id, trigger_type, status, created_at) "
                "VALUES (1, 'manual', 'running', datetime('now'))"
            )
            conn.commit()

        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        await proc.stop()

        with self.pool.connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT status FROM wiki_compile_jobs").fetchone()
        # After start+stop, the orphan should have been reset to pending (then possibly processed)
        self.assertNotEqual(dict(row)["status"], "running")


# ---------------------------------------------------------------------------
# retry_count tracking and cancellation semantics
# ---------------------------------------------------------------------------

class TestRetryCountAndCancellation(unittest.TestCase):

    def setUp(self):
        from app.models.database import run_migrations
        from app.services.wiki_store import WikiStore

        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "test.db")
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
        self.conn.commit()
        self.store = WikiStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_fail_job_increments_retry_count(self):
        job = self.store.create_job(vault_id=1, trigger_type="manual")
        self.store.claim_next_pending_job()
        count1 = self.store.fail_job(job.id, "err1")
        self.assertEqual(count1, 1)
        # Reset to pending and fail again
        self.store.reset_job_to_pending(job.id)
        self.store.claim_next_pending_job()
        count2 = self.store.fail_job(job.id, "err2")
        self.assertEqual(count2, 2)

    def test_fail_job_returns_retry_count(self):
        job = self.store.create_job(vault_id=1, trigger_type="manual")
        self.store.claim_next_pending_job()
        returned = self.store.fail_job(job.id, "error")
        row = self.conn.execute(
            "SELECT retry_count FROM wiki_compile_jobs WHERE id = ?", (job.id,)
        ).fetchone()
        self.assertEqual(returned, dict(row)["retry_count"])

    def test_cancelled_job_not_overwritten_by_complete(self):
        job = self.store.create_job(vault_id=1, trigger_type="manual")
        self.store.claim_next_pending_job()
        # Cancel while running
        cancelled = self.store.cancel_job(job.id, vault_id=1)
        self.assertTrue(cancelled)
        # Processor tries to complete it after cancellation — must be no-op
        self.store.complete_job(job.id, {"ok": True})
        row = self.conn.execute(
            "SELECT status FROM wiki_compile_jobs WHERE id = ?", (job.id,)
        ).fetchone()
        self.assertEqual(dict(row)["status"], "cancelled")

    def test_wiki_job_dataclass_has_retry_count(self):
        job = self.store.create_job(vault_id=1, trigger_type="manual")
        fetched = self.store.get_job(job.id, vault_id=1)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.retry_count, 0)
        self.store.claim_next_pending_job()
        self.store.fail_job(job.id, "boom")
        fetched2 = self.store.get_job(job.id, vault_id=1)
        self.assertEqual(fetched2.retry_count, 1)

    def test_fail_job_does_not_overwrite_cancelled(self):
        """fail_job must not change a cancelled job's status to failed."""
        job = self.store.create_job(vault_id=1, trigger_type="manual")
        self.store.claim_next_pending_job()
        # Cancel while running
        self.store.cancel_job(job.id, vault_id=1)
        # Processor raises and calls fail_job — must be a no-op
        self.store.fail_job(job.id, "handler raised")
        row = self.conn.execute(
            "SELECT status, retry_count FROM wiki_compile_jobs WHERE id = ?", (job.id,)
        ).fetchone()
        self.assertEqual(dict(row)["status"], "cancelled")
        self.assertEqual(dict(row)["retry_count"], 0)


# ---------------------------------------------------------------------------
# Per-claim citation precision
# ---------------------------------------------------------------------------

class TestPerClaimCitationPrecision(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_cited_claim_is_active_uncited_is_unverified(self):
        from app.services.wiki_compiler import extract_entities_from_text
        ext = extract_entities_from_text(TWO_CLAIM_ANSWER)
        role_claims = ext.role_claims
        self.assertGreaterEqual(len(role_claims), 2, "Need ≥2 role claims in TWO_CLAIM_ANSWER")

        cited_sentence = role_claims[0]["sentence"]
        # Only the first claim sentence gets a citation
        per_claim_sources = {
            cited_sentence: [{"source_kind": "document", "source_label": "S1", "file_id": 1}]
        }
        result = self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": TWO_CLAIM_ANSWER,
                "per_claim_sources": per_claim_sources,
            },
        )
        statuses = {c["status"] for c in result["claims"]}
        self.assertIn("active", statuses, "Cited claim must be active")
        self.assertIn("unverified", statuses, "Uncited claim must be unverified")

    def test_only_cited_sources_attached_to_active_claim(self):
        from app.services.wiki_compiler import extract_entities_from_text
        ext = extract_entities_from_text(TWO_CLAIM_ANSWER)
        role_claims = ext.role_claims
        self.assertGreaterEqual(len(role_claims), 2)

        s1_sentence = role_claims[0]["sentence"]
        s2_sentence = role_claims[1]["sentence"]
        per_claim_sources = {
            s1_sentence: [{"source_kind": "document", "source_label": "S1", "file_id": 1}],
            s2_sentence: [{"source_kind": "memory", "source_label": "M1", "memory_id": 1}],
        }
        result = self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": TWO_CLAIM_ANSWER,
                "per_claim_sources": per_claim_sources,
            },
        )
        # Find the two claims
        all_claims = result["claims"]
        self.assertEqual(len(all_claims), 2)
        claim_ids = [c["id"] for c in all_claims]
        # Check sources for each claim
        for cid in claim_ids:
            sources = self.conn.execute(
                "SELECT source_kind, source_label FROM wiki_claim_sources WHERE claim_id = ?",
                (cid,),
            ).fetchall()
            source_labels = {dict(s)["source_label"] for s in sources}
            # Each claim should have exactly ONE source, not both S1 and M1
            self.assertEqual(len(source_labels), 1)

    def test_answer_level_refs_attached_when_no_per_claim_sources(self):
        result = self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": AFOMIS_ANSWER,
                "wiki_refs": [{"wiki_label": "W1"}],
                "doc_sources": [],
                "memories": [],
            },
        )
        # Without per_claim_sources: all claims are unverified
        statuses = {c["status"] for c in result["claims"]}
        self.assertTrue(all(s == "unverified" for s in statuses))
        # But sources are still attached for context
        for c in result["claims"]:
            sources = self.conn.execute(
                "SELECT source_label FROM wiki_claim_sources WHERE claim_id = ?", (c["id"],)
            ).fetchall()
            labels = {dict(s)["source_label"] for s in sources}
            self.assertIn("W1", labels)


# ---------------------------------------------------------------------------
# Idempotent compilation (issues 4 + 6)
# ---------------------------------------------------------------------------

class TestIdempotentCompilation(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_recompile_ingest_does_not_duplicate_claims(self):
        inp = {"file_id": 1, "text": AFOMIS_ANSWER}
        r1 = self.compiler.compile_ingest_job(vault_id=1, input_json=inp)
        self.compiler.compile_ingest_job(vault_id=1, input_json=inp)
        count_after_two = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()[0]
        unique_after_one = len({c["id"] for c in r1["claims"]})
        self.assertEqual(count_after_two, unique_after_one, "Recompile must not duplicate claims")

    def test_recompile_ingest_does_not_duplicate_relations(self):
        inp = {"file_id": 1, "text": AFOMIS_ANSWER}
        self.compiler.compile_ingest_job(vault_id=1, input_json=inp)
        self.compiler.compile_ingest_job(vault_id=1, input_json=inp)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_relations WHERE vault_id = 1"
        ).fetchone()[0]
        # Same relations as after first compile
        self.compiler.compile_ingest_job(vault_id=1, input_json=inp)
        count_after_third = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_relations WHERE vault_id = 1"
        ).fetchone()[0]
        self.assertEqual(count, count_after_third)

    def test_recompile_query_does_not_duplicate_claims(self):
        inp = {
            "assistant_answer": AFOMIS_ANSWER,
            "wiki_refs": [],
            "doc_sources": [],
            "memories": [],
        }
        r1 = self.compiler.compile_query_job(vault_id=1, input_json=inp)
        self.compiler.compile_query_job(vault_id=1, input_json=inp)
        count_after_two = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()[0]
        unique_after_one = len({c["id"] for c in r1["claims"]})
        self.assertEqual(count_after_two, unique_after_one, "Recompile must not duplicate claims")

    def test_promote_memory_twice_no_duplicate_claims(self):
        # Insert a real memory row
        self.conn.execute(
            "INSERT OR IGNORE INTO memories (id, vault_id, content) VALUES (1, 1, ?)",
            (AFOMIS_ANSWER,),
        )
        self.conn.commit()
        r1 = self.compiler.promote_memory(memory_id=1, vault_id=1)
        self.compiler.promote_memory(memory_id=1, vault_id=1)
        count_after_two = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()[0]
        unique_after_one = len({c.id for c in r1["claims"]})
        self.assertEqual(count_after_two, unique_after_one, "Re-promotion must not duplicate claims")

    def test_promote_memory_twice_no_duplicate_relations(self):
        self.conn.execute(
            "INSERT OR IGNORE INTO memories (id, vault_id, content) VALUES (1, 1, ?)",
            (AFOMIS_ANSWER,),
        )
        self.conn.commit()
        self.compiler.promote_memory(memory_id=1, vault_id=1)
        self.compiler.promote_memory(memory_id=1, vault_id=1)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_relations WHERE vault_id = 1"
        ).fetchone()[0]
        self.compiler.promote_memory(memory_id=1, vault_id=1)
        count_after_third = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_relations WHERE vault_id = 1"
        ).fetchone()[0]
        self.assertEqual(count, count_after_third)


# ---------------------------------------------------------------------------
# compile_ingest_job text fallback to files.parsed_text (issue 1)
# ---------------------------------------------------------------------------

class TestIngestJobParsedTextFallback(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_fallback_to_parsed_text_when_no_text_in_input_json(self):
        # Set parsed_text on the files row (simulates what document_processor now saves)
        self.conn.execute(
            "UPDATE files SET parsed_text = ? WHERE id = 1", (AFOMIS_ANSWER,)
        )
        self.conn.commit()
        # input_json has no "text" key — compiler must fall back to files.parsed_text
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1}
        )
        self.assertFalse(result.get("skipped"))
        self.assertGreater(len(result["claims"]), 0)

    def test_input_json_text_takes_precedence_over_parsed_text(self):
        # Set parsed_text to empty/wrong content
        self.conn.execute(
            "UPDATE files SET parsed_text = 'no entities here' WHERE id = 1"
        )
        self.conn.commit()
        # input_json has explicit text with entities — that must win
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )
        self.assertFalse(result.get("skipped"))
        entity_names = {e["name"] for e in result["entities"]}
        self.assertIn("AFOMIS", entity_names)


# ---------------------------------------------------------------------------
# Issue 1 regression: existing indexed doc with parsed_text=NULL re-parses
# ---------------------------------------------------------------------------

class TestIngestJobReParseFromFilePath(unittest.TestCase):
    """Pre-migration docs with parsed_text=NULL must re-parse from file_path."""

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()
        # Create a real .txt file with extractable content (no unstructured needed)
        self._tmpfile = tempfile.NamedTemporaryFile(
            suffix=".txt", mode="w", delete=False, encoding="utf-8"
        )
        self._tmpfile.write(AFOMIS_ANSWER)
        self._tmpfile.close()
        self.conn.execute(
            "UPDATE files SET file_path = ?, file_name = ? WHERE id = 1",
            (self._tmpfile.name, "afomis.txt"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import os
        try:
            os.unlink(self._tmpfile.name)
        except OSError:
            pass

    def test_existing_indexed_file_null_parsed_text_compiles_from_file_path(self):
        """Regression: pre-migration file with parsed_text=NULL + valid file_path → wiki output."""
        # Confirm parsed_text is NULL (default from _make_env)
        row = self.conn.execute("SELECT parsed_text FROM files WHERE id = 1").fetchone()
        self.assertIsNone(row[0], "parsed_text should start as NULL for this test")

        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1}  # manual compile — no text supplied
        )
        self.assertFalse(result.get("skipped"), "Should not skip: valid .txt file_path available")
        self.assertGreater(len(result["claims"]), 0, "Should produce wiki claims")

    def test_re_parse_caches_parsed_text_for_future_compiles(self):
        """After re-parse, files.parsed_text should be populated."""
        self.compiler.compile_ingest_job(vault_id=1, input_json={"file_id": 1})
        row = self.conn.execute("SELECT parsed_text FROM files WHERE id = 1").fetchone()
        self.assertIsNotNone(row[0], "parsed_text should be cached after re-parse")
        self.assertIn("AFOMIS", row[0])


# ---------------------------------------------------------------------------
# Issue 2: chunk_id populated for document-derived claims
# ---------------------------------------------------------------------------

class TestIngestJobChunkProvenance(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_chunk_id_populated_for_document_claims(self):
        """wiki_claim_sources.chunk_id must be set for every document-derived claim."""
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )
        self.assertGreater(len(result["claims"]), 0)
        for c in result["claims"]:
            sources = self.conn.execute(
                "SELECT chunk_id FROM wiki_claim_sources "
                "WHERE claim_id = ? AND source_kind = 'document'",
                (c["id"],),
            ).fetchall()
            self.assertGreater(len(sources), 0, f"Claim {c['id']} has no document sources")
            for src in sources:
                cid = dict(src)["chunk_id"]
                self.assertIsNotNone(cid, f"chunk_id is NULL for claim {c['id']}")
                self.assertIn("_", cid, "chunk_id must follow '{file_id}_{chunk_index}' format")

    def test_chunk_id_deterministic_on_recompile(self):
        """Recompile must not change chunk_ids (same text → same position → same uid)."""
        inp = {"file_id": 1, "text": AFOMIS_ANSWER}
        self.compiler.compile_ingest_job(vault_id=1, input_json=inp)
        ids_1 = {
            dict(r)["chunk_id"]
            for r in self.conn.execute(
                "SELECT chunk_id FROM wiki_claim_sources WHERE source_kind = 'document'"
            ).fetchall()
        }
        self.compiler.compile_ingest_job(vault_id=1, input_json=inp)
        ids_2 = {
            dict(r)["chunk_id"]
            for r in self.conn.execute(
                "SELECT chunk_id FROM wiki_claim_sources WHERE source_kind = 'document'"
            ).fetchall()
        }
        self.assertEqual(ids_1, ids_2, "chunk_ids must be deterministic across recompiles")


# ---------------------------------------------------------------------------
# Issue 3: _build_per_claim_sources handles trailing / inline citations
# ---------------------------------------------------------------------------

class TestBuildPerClaimSourcesTrailingCitations(unittest.TestCase):
    """Unit tests for the citation-to-sentence attribution logic in chat.py."""

    def _call(self, answer, doc_sources=None, mems=None, wiki=None):
        from app.services.wiki_citation_helpers import build_per_claim_sources
        return build_per_claim_sources(
            answer, doc_sources or [], mems or [], wiki or []
        )

    def _doc(self):
        return [{"source_label": "S1", "file_id": 1}]

    def test_trailing_citation_attaches_to_preceding_sentence(self):
        """'Claim. [S1]' — standalone [S1] must be attributed to 'Claim.'"""
        result = self._call(
            "Justice Sakyi is the AFOMIS Chief. [S1]", doc_sources=self._doc()
        )
        matching = [k for k in result if "Justice Sakyi" in k]
        self.assertEqual(len(matching), 1, f"Expected 1 key matching claim, got: {list(result)}")
        self.assertEqual(result[matching[0]][0]["source_kind"], "document")

    def test_inline_citation_before_period(self):
        """'Claim [S1].' — inline citation before period must be attributed."""
        result = self._call(
            "Justice Sakyi is the AFOMIS Chief [S1].", doc_sources=self._doc()
        )
        matching = [k for k in result if "Justice Sakyi" in k]
        self.assertEqual(len(matching), 1, f"Expected 1 key matching claim, got: {list(result)}")
        self.assertEqual(result[matching[0]][0]["source_kind"], "document")

    def test_two_claims_with_trailing_mixed_citations(self):
        """'Claim A. [S1] Claim B. [M1]' produces two separate source entries."""
        doc = [{"source_label": "S1", "file_id": 1}]
        mem = [{"memory_label": "M1", "id": "1"}]
        result = self._call(
            "Justice Sakyi is the AFOMIS Chief. [S1] "
            "Major Justin Woods is the AFOMIS Deputy. [M1]",
            doc_sources=doc,
            mems=mem,
        )
        self.assertEqual(len(result), 2, f"Expected 2 source entries, got: {list(result)}")
        doc_keys = [k for k in result if "Justice Sakyi" in k]
        mem_keys = [k for k in result if "Justin Woods" in k]
        self.assertEqual(len(doc_keys), 1)
        self.assertEqual(len(mem_keys), 1)
        self.assertEqual(result[doc_keys[0]][0]["source_kind"], "document")
        self.assertEqual(result[mem_keys[0]][0]["source_kind"], "memory")

    def test_citation_only_segment_does_not_create_orphan_key(self):
        """No key should consist solely of citation markers."""
        result = self._call(
            "Justice Sakyi is the AFOMIS Chief. [S1]", doc_sources=self._doc()
        )
        for key in result:
            self.assertFalse(
                key.strip().startswith("["),
                f"Orphan citation-only key found: {key!r}",
            )


# ---------------------------------------------------------------------------
# Issue 4: unverified claim promoted to active by later cited query job
# ---------------------------------------------------------------------------

class TestUnverifiedClaimPromotion(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_unverified_claim_promoted_to_active_by_cited_query(self):
        """An unverified claim is promoted to active when a cited query later references it.

        Ingest now creates active claims directly, so this test creates an unverified
        claim explicitly via the store to keep the promotion path covered.
        """
        from app.services.wiki_compiler import extract_entities_from_text

        # Step 1: extract the sentence text that compile_query_job will look up
        ext = extract_entities_from_text(AFOMIS_ANSWER)
        self.assertGreater(len(ext.role_claims), 0)
        cited_sentence = ext.role_claims[0]["sentence"]

        # Create an unverified claim directly (simulates a pre-migration or
        # externally inserted claim that has not yet been cited by any query).
        self.store.create_claim(
            vault_id=1,
            claim_text=cited_sentence,
            source_type="document",
            claim_type="fact",
            status="unverified",
            confidence=0.5,
        )
        self.conn.commit()

        # Confirm it starts as unverified
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE vault_id = 1 AND claim_text = ?",
            (cited_sentence,),
        ).fetchone()
        self.assertEqual(dict(row)["status"], "unverified")

        # Step 2: query job cites the same sentence → should promote to active
        per_claim_sources = {
            cited_sentence: [{"source_kind": "document", "source_label": "S1", "file_id": 1}]
        }
        self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": AFOMIS_ANSWER,
                "per_claim_sources": per_claim_sources,
            },
        )

        # Step 3: the cited claim must now be active AND have higher confidence.
        # Asserting confidence change proves the promotion branch executed (not just
        # that the claim happened to already be active by some other path).
        row = self.conn.execute(
            "SELECT status, confidence FROM wiki_claims WHERE vault_id = 1 AND claim_text = ?",
            (cited_sentence,),
        ).fetchone()
        self.assertEqual(dict(row)["status"], "active", "Cited claim should be promoted to active")
        self.assertGreater(dict(row)["confidence"], 0.5, "Confidence must increase on promotion")

    def test_already_active_claim_not_demoted(self):
        """A claim that is already active must not be changed by a second compile."""
        from app.services.wiki_compiler import extract_entities_from_text

        ext = extract_entities_from_text(AFOMIS_ANSWER)
        cited_sentence = ext.role_claims[0]["sentence"]
        per_claim_sources = {
            cited_sentence: [{"source_kind": "document", "source_label": "S1", "file_id": 1}]
        }
        inp = {"assistant_answer": AFOMIS_ANSWER, "per_claim_sources": per_claim_sources}
        # First compile: creates active claim
        self.compiler.compile_query_job(vault_id=1, input_json=inp)
        # Second compile: must not demote to unverified
        self.compiler.compile_query_job(vault_id=1, input_json=inp)
        all_statuses = {
            dict(r)["status"]
            for r in self.conn.execute(
                "SELECT status FROM wiki_claims WHERE vault_id = 1"
            ).fetchall()
        }
        self.assertNotIn("unverified", all_statuses, "Active claim must not be demoted")


# ---------------------------------------------------------------------------
# Issue 5: attach all missing sources for duplicate claims
# ---------------------------------------------------------------------------

class TestAttachAllMissingSources(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()
        self.conn.execute(
            "INSERT OR IGNORE INTO memories (id, vault_id, content) VALUES (1, 1, 'test')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_both_sources_attached_for_double_cited_claim(self):
        """[S1][M1] double citation: both document and memory sources attached exactly once."""
        from app.services.wiki_compiler import extract_entities_from_text

        ext = extract_entities_from_text(AFOMIS_ANSWER)
        cited_sentence = ext.role_claims[0]["sentence"]
        per_claim_sources = {
            cited_sentence: [
                {"source_kind": "document", "source_label": "S1", "file_id": 1},
                {"source_kind": "memory", "source_label": "M1", "memory_id": 1},
            ]
        }
        inp = {"assistant_answer": AFOMIS_ANSWER, "per_claim_sources": per_claim_sources}

        # Run compile twice — second run must not add duplicate sources
        r1 = self.compiler.compile_query_job(vault_id=1, input_json=inp)
        self.compiler.compile_query_job(vault_id=1, input_json=inp)

        # Get one of the claim IDs with the cited sentence
        active_claim_ids = [
            c["id"] for c in r1["claims"] if c["status"] == "active"
        ]
        self.assertGreater(len(active_claim_ids), 0, "Expected at least one active claim")
        claim_id = active_claim_ids[0]

        sources = self.conn.execute(
            "SELECT source_kind FROM wiki_claim_sources WHERE claim_id = ?", (claim_id,)
        ).fetchall()
        kinds = [dict(s)["source_kind"] for s in sources]
        self.assertEqual(kinds.count("document"), 1, "Exactly 1 document source expected")
        self.assertEqual(kinds.count("memory"), 1, "Exactly 1 memory source expected")

    def test_no_double_attach_when_ingest_first_then_query_cites(self):
        """
        Existing claim (from ingest, document source) + query citing same claim with
        BOTH [S1][M1] must produce exactly 1 document + 1 memory source, not 2 document.
        This guards against the stale-snapshot race where _find_or_create_claim attaches
        [S1] and returns a stale sources list; the outer loop must not re-attach [S1].
        """
        from app.services.wiki_compiler import extract_entities_from_text

        # Step 1: ingest creates the claim with a document source
        self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )

        # Step 2: query job cites the same sentence with [S1][M1]
        ext = extract_entities_from_text(AFOMIS_ANSWER)
        cited_sentence = ext.role_claims[0]["sentence"]
        per_claim_sources = {
            cited_sentence: [
                {"source_kind": "document", "source_label": "S1", "file_id": 1},
                {"source_kind": "memory", "source_label": "M1", "memory_id": 1},
            ]
        }
        r = self.compiler.compile_query_job(
            vault_id=1,
            input_json={"assistant_answer": AFOMIS_ANSWER, "per_claim_sources": per_claim_sources},
        )

        claim_ids = [c["id"] for c in r["claims"]]
        for cid in claim_ids:
            sources = self.conn.execute(
                "SELECT source_kind FROM wiki_claim_sources WHERE claim_id = ?", (cid,)
            ).fetchall()
            kinds = [dict(s)["source_kind"] for s in sources]
            self.assertLessEqual(kinds.count("document"), 1, f"Duplicate document source on claim {cid}")

    def test_no_duplicate_sources_on_recompile(self):
        """Recompiling with same per_claim_sources must not create extra source rows."""
        from app.services.wiki_compiler import extract_entities_from_text

        ext = extract_entities_from_text(AFOMIS_ANSWER)
        cited_sentence = ext.role_claims[0]["sentence"]
        per_claim_sources = {
            cited_sentence: [{"source_kind": "document", "source_label": "S1", "file_id": 1}]
        }
        inp = {"assistant_answer": AFOMIS_ANSWER, "per_claim_sources": per_claim_sources}

        self.compiler.compile_query_job(vault_id=1, input_json=inp)
        count_after_1 = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claim_sources"
        ).fetchone()[0]
        self.compiler.compile_query_job(vault_id=1, input_json=inp)
        count_after_2 = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claim_sources"
        ).fetchone()[0]
        self.assertEqual(count_after_1, count_after_2, "No new source rows should appear on recompile")

    def test_ingest_doc_source_then_query_memory_only_citation(self):
        """
        Stale-snapshot regression (Finding E): pre-created claim (document source
        via ingest) + query citing same sentence with memory-only citation must
        produce exactly 1 document + 1 memory source with no duplicates.

        Without the reload fix, _find_or_create_claim attaches the memory source
        and returns a stale existing.sources (pre-commit snapshot). The outer loop
        then re-attaches the same memory source, producing a duplicate.
        """
        from app.services.wiki_compiler import extract_entities_from_text

        # Step 1: ingest creates claim with document source
        self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )

        # Step 2: query cites same sentence with memory-only citation
        ext = extract_entities_from_text(AFOMIS_ANSWER)
        cited_sentence = ext.role_claims[0]["sentence"]
        per_claim_sources = {
            cited_sentence: [{"source_kind": "memory", "source_label": "M1", "memory_id": 1}]
        }
        r = self.compiler.compile_query_job(
            vault_id=1,
            input_json={"assistant_answer": AFOMIS_ANSWER, "per_claim_sources": per_claim_sources},
        )

        # Cited claim should be promoted to active
        active_claims = [c for c in r["claims"] if c["status"] == "active"]
        self.assertGreater(len(active_claims), 0, "Claim should be promoted to active by memory citation")

        # Must have exactly 1 document + 1 memory source (no duplicates)
        claim_id = active_claims[0]["id"]
        sources = self.conn.execute(
            "SELECT source_kind FROM wiki_claim_sources WHERE claim_id = ?", (claim_id,)
        ).fetchall()
        kinds = [dict(s)["source_kind"] for s in sources]
        self.assertEqual(kinds.count("document"), 1, "Exactly 1 document source expected (from ingest)")
        self.assertEqual(kinds.count("memory"), 1, "Exactly 1 memory source expected (from query citation)")


# ---------------------------------------------------------------------------
# Wiki-first retrieval gate integration
# ---------------------------------------------------------------------------

class TestWikiFirstRetrievalGate(unittest.TestCase):
    """Verify that document-ingested claims satisfy the RAGEngine wiki-first gate.

    The gate (_raw_rag_required) skips raw document RAG for entity_lookup queries
    when wiki evidence contains at least one claim that is:
      - status in ("active", "verified")
      - page_status NOT in ("stale", "archived", "draft")
      - confidence >= 0.75
    compile_ingest_job must produce claims that meet all three criteria.
    """

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def _make_wiki_evidence(self, claim_status, confidence, page_status="needs_review", freshness=None):
        from datetime import datetime as _dt

        from app.services.wiki_retrieval import WikiEvidence
        if freshness is None:
            freshness = _dt.utcnow().isoformat()
        return WikiEvidence(
            label_placeholder="W1",
            page_id=1,
            claim_id=1,
            title="AFOMIS",
            slug="acronym/afomis",
            page_type="acronym",
            claim_text="Justice Sakyi is the AFOMIS Chief.",
            excerpt="",
            confidence=confidence,
            page_status=page_status,
            claim_status=claim_status,
            score=confidence,
            score_type="relation",
            freshness=freshness,
            source_count=1,
            provenance_summary="1 doc",
        )

    def test_ingest_creates_active_claim_with_sufficient_confidence(self):
        """Document-ingested claims must be status='active' and confidence>=0.75."""
        result = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": AFOMIS_ANSWER}
        )
        self.assertGreater(len(result["claims"]), 0)
        for c in result["claims"]:
            self.assertEqual(c["status"], "active", "Document-ingest claims must be active")
            row = self.conn.execute(
                "SELECT confidence FROM wiki_claims WHERE id = ?", (c["id"],)
            ).fetchone()
            self.assertGreaterEqual(
                dict(row)["confidence"], 0.75,
                "Document-ingest confidence must be ≥0.75 to satisfy the wiki-first gate",
            )

    def test_active_document_claim_satisfies_wiki_first_gate(self):
        """Active document claim with confidence=0.8 and needs_review page: gate skips raw RAG."""
        from app.services.rag_engine import _raw_rag_required
        evidence = self._make_wiki_evidence(claim_status="active", confidence=0.8)
        result = _raw_rag_required("entity_lookup", [evidence])
        self.assertFalse(result, "Active provenance-backed document claim must not require raw RAG")

    def test_needs_review_page_status_allowed_by_gate(self):
        """Page status 'needs_review' is not in the blocked set — wiki-first must proceed."""
        from app.services.rag_engine import _raw_rag_required
        evidence = self._make_wiki_evidence(
            claim_status="active", confidence=0.8, page_status="needs_review"
        )
        result = _raw_rag_required("entity_lookup", [evidence])
        self.assertFalse(result, "needs_review page must not block wiki-first retrieval")

    def test_unverified_claim_still_requires_raw_rag(self):
        """Both gate conditions must hold independently.

        An unverified claim is blocked regardless of confidence; an active claim
        below the 0.75 confidence threshold is also blocked.
        """
        from app.services.rag_engine import _raw_rag_required
        # Low-confidence unverified: blocked
        ev = self._make_wiki_evidence(claim_status="unverified", confidence=0.7)
        self.assertTrue(_raw_rag_required("entity_lookup", [ev]),
                        "Unverified low-confidence claim must require raw RAG")
        # High-confidence unverified: status alone blocks (regardless of confidence)
        ev2 = self._make_wiki_evidence(claim_status="unverified", confidence=0.9)
        self.assertTrue(_raw_rag_required("entity_lookup", [ev2]),
                        "Unverified high-confidence claim must still require raw RAG")
        # Active but below confidence threshold: confidence alone blocks
        ev3 = self._make_wiki_evidence(claim_status="active", confidence=0.7)
        self.assertTrue(_raw_rag_required("entity_lookup", [ev3]),
                        "Active claim below 0.75 confidence must require raw RAG")

    def test_no_wiki_evidence_requires_raw_rag(self):
        """Empty evidence (e.g., AFMEDCOM query matches no AFOMIS entity) → raw RAG required."""
        from app.services.rag_engine import _raw_rag_required
        result = _raw_rag_required("entity_lookup", [])
        self.assertTrue(result, "No wiki evidence must always require raw RAG")

    def test_stale_page_blocked_even_with_active_claim(self):
        """Stale page status must block wiki-first even when the claim itself is active."""
        from app.services.rag_engine import _raw_rag_required
        evidence = self._make_wiki_evidence(
            claim_status="active", confidence=0.8, page_status="stale"
        )
        result = _raw_rag_required("entity_lookup", [evidence])
        self.assertTrue(result, "Stale page must require raw RAG regardless of claim status")

    def test_stale_freshness_requires_raw_rag(self):
        """Old freshness timestamp must not suppress raw RAG even with active high-confidence claim."""
        from app.services.rag_engine import _raw_rag_required
        evidence = self._make_wiki_evidence(
            claim_status="active", confidence=0.9, freshness="2020-01-01T00:00:00"
        )
        result = _raw_rag_required("entity_lookup", [evidence])
        self.assertTrue(result, "Stale freshness must require raw RAG")
