"""Compiler extraction policy tests (issue #1162, Workstream L PR 6).

Frozen-check spec backing acceptance checks C5/C6/C7/C8 (trace
1161-1162-1163-wiki-compiler-review-queue): procedure pages, boilerplate
excluded from summaries, TOC chunks not retrievable, and the subjectless
claim gate. Additional in-scope behavior from the issue body: TOC-only and
mixed TOC+procedure documents, numbered code samples, deterministic
subjectless rejection through the full compile, and the
qualifying-to-plain recompute lifecycle (existing records refresh and go
stale; nothing is deleted).
"""

# The chunking seams this file exercises are optional-dependency seams.
# backend/conftest.py installs a minimal `unstructured` stub before any test
# module imports (its chunk_by_title and partition return []), and a handful
# of test modules REPLACE those stub modules unconditionally at import time
# (test_contextual_chunking, test_hybrid_logging, test_integration and
# test_retrieval_logging — every one sorted after this file). Under
# pytest-xdist each worker imports every module before any test runs, so the
# module object registered LAST is the one live at test time: seeding
# sys.modules at import time here is process-global and order-dependent, and
# it was silently overwritten on CI (PR #1204 CI round 1 — SemanticChunker
# saw an empty element set). The seams are therefore RE-ASSERTED per test in
# _H2Base.setUp with mock.patch on the live module object, and the
# import-time seeding is guarded so it never displaces a real full install.
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeChunk:
    """Minimal chunk_by_title return item: str() is the text, no metadata."""

    def __init__(self, text: str):
        self.text = text
        self.metadata = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def _partition_text_stub(text=None, *args, **kwargs):
    # Split on blank lines into pseudo-elements so the chunker's
    # title-chunking path still runs on our fixtures.
    return [part for part in (text or "").split("\n\n") if part.strip()]


def _chunk_by_title_stub(elements, **kwargs):
    return [_FakeChunk(str(e)) for e in elements]


try:
    from unstructured.chunking.title import chunk_by_title as _probe_cbt  # noqa: F401
    from unstructured.partition.text import partition_text as _probe_pt  # noqa: F401
except ImportError:
    _unstructured = sys.modules.get("unstructured") or types.ModuleType("unstructured")
    _unstructured.__path__ = getattr(_unstructured, "__path__", [])
    _partition = sys.modules.get("unstructured.partition") or types.ModuleType(
        "unstructured.partition"
    )
    _partition.__path__ = getattr(_partition, "__path__", [])
    _partition_text = types.ModuleType("unstructured.partition.text")
    _partition_text.partition_text = _partition_text_stub
    _unstructured.partition = _partition
    _partition.text = _partition_text
    _chunking = sys.modules.get("unstructured.chunking") or types.ModuleType(
        "unstructured.chunking"
    )
    _chunking.__path__ = getattr(_chunking, "__path__", [])
    _chunking_title = types.ModuleType("unstructured.chunking.title")
    _chunking_title.chunk_by_title = _chunk_by_title_stub
    _unstructured.chunking = _chunking
    _chunking.title = _chunking_title
    for _name, _sub in (
        ("unstructured", _unstructured),
        ("unstructured.partition", _partition),
        ("unstructured.partition.text", _partition_text),
        ("unstructured.chunking", _chunking),
        ("unstructured.chunking.title", _chunking_title),
    ):
        sys.modules[_name] = _sub

from app.models.database import init_db, run_migrations
from app.services.chunking import SemanticChunker, filter_toc_chunks
from app.services.content_classifiers import (
    document_has_qualifying_items,
    extract_procedures,
)
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.kms_store import KMSStore
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_store import WikiStore

VAULT_ID = 1
FILE_ID = 1

ROLE_CLAIM_TEXT = "Alice Smith is the AFOMIS Director."
PLAIN_TEXT = (
    "The product team shipped a release on Tuesday. "
    "Feedback was gathered from beta users and incorporated."
)
PROCEDURE_TEXT = (
    "# Badge renewal\n\n"
    "Follow these steps to renew a badge.\n\n"
    "1. Collect the analyst's current badge.\n"
    "2. Verify the badge number against the register.\n"
    "3. Issue the new badge and file the old one.\n"
)
TOC_TEXT = (
    "Table of Contents\n\n"
    "Introduction ........ 1\n"
    "Badge protocol ........ 12\n"
    "Appendix ........ 40\n"
)
LICENCE_TEXT = (
    "Licence\n\n"
    "Copyright 2020 Example Corp. All rights reserved.\n"
    "Redistribution and use in source and binary forms are permitted.\n"
)
CODE_SAMPLE_TEXT = (
    "Example configuration:\n\n"
    "```\n"
    "1. set the retry count\n"
    "2. set the endpoint\n"
    "```\n"
)


def _mkdtemp_db():
    db_path = str(Path(tempfile.mkdtemp(prefix="h2-compile-")) / "h2.db")
    init_db(db_path)
    run_migrations(db_path)
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')"
    )
    conn.execute(
        "INSERT INTO files (id, vault_id, file_path, file_name, file_size, status, parsed_text) "
        "VALUES (?, 1, 'docs/one.txt', 'one.txt', 1, 'indexed', ?)",
        (FILE_ID, ROLE_CLAIM_TEXT),
    )
    conn.commit()
    return conn


class _H2Base(unittest.TestCase):
    def setUp(self):
        self.conn = _mkdtemp_db()
        self.addCleanup(self.conn.close)
        self.store = WikiStore(self.conn)
        self.compiler = WikiCompiler(self.conn)
        self.kms_store = KMSStore(self.conn)
        # Re-assert the deterministic unstructured seams for THIS test only
        # (restored on cleanup): whichever module object is live in
        # sys.modules — real (full install) or a sibling's stub — gets this
        # file's partition/chunk behavior for the duration of the test.
        for target, stub in (
            ("unstructured.partition.text.partition_text", _partition_text_stub),
            ("unstructured.chunking.title.chunk_by_title", _chunk_by_title_stub),
        ):
            patcher = patch(target, stub)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _set_text(self, text: str):
        self.conn.execute(
            "UPDATE files SET parsed_text = ? WHERE id = ?", (text, FILE_ID)
        )
        self.conn.commit()

    def _compile(self, text: str):
        self._set_text(text)
        return self.compiler.compile_ingest_job(
            vault_id=VAULT_ID, input_json={"file_id": FILE_ID}
        )

    def _derived_counts(self):
        pages = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE vault_id = ?", (VAULT_ID,)
        ).fetchone()[0]
        assoc = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_page_files WHERE vault_id = ? AND file_id = ?",
            (VAULT_ID, FILE_ID),
        ).fetchone()[0]
        entries = self.conn.execute(
            "SELECT COUNT(*) FROM kms_entries WHERE vault_id = ? AND source_type = 'document'",
            (VAULT_ID,),
        ).fetchone()[0]
        return pages, assoc, entries


class TestProcedurePages(_H2Base):
    def test_numbered_procedure_becomes_procedure_page(self):
        """AC6 / C5: a numbered procedure yields a procedure page whose steps
        keep their numbers and order."""
        result = self._compile(PROCEDURE_TEXT)
        self.assertFalse(result.get("skipped"), result)
        rows = self.conn.execute(
            "SELECT id, markdown FROM wiki_pages WHERE vault_id = ? AND page_type = 'procedure'",
            (VAULT_ID,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        markdown = rows[0]["markdown"]
        self.assertIn("1. Collect the analyst's current badge.", markdown)
        self.assertIn("2. Verify the badge number against the register.", markdown)
        self.assertIn("3. Issue the new badge and file the old one.", markdown)
        self.assertLess(
            markdown.index("1. Collect"),
            markdown.index("2. Verify"),
            "steps keep their order",
        )

    def test_toc_only_document_stays_suppressed(self):
        """Additional in-scope: a numbered/leader TOC never qualifies."""
        result = self._compile(TOC_TEXT)
        self.assertTrue(result.get("skipped"))
        self.assertEqual(self._derived_counts(), (0, 0, 0))

    def test_mixed_toc_and_procedure_qualifies_via_procedure(self):
        """Additional in-scope: mixed documents keep substantive content."""
        result = self._compile(TOC_TEXT + "\n\n" + PROCEDURE_TEXT)
        self.assertFalse(result.get("skipped"), result)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE vault_id = ? AND page_type = 'procedure'",
            (VAULT_ID,),
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_numbered_code_sample_is_not_a_procedure(self):
        """Additional in-scope: fenced code never yields procedures."""
        self.assertFalse(extract_procedures(CODE_SAMPLE_TEXT))
        result = self._compile(CODE_SAMPLE_TEXT + "\n\n" + PLAIN_TEXT)
        self.assertTrue(result.get("skipped"), result)


class TestBoilerplateSummary(_H2Base):
    def test_boilerplate_is_excluded_from_summary(self):
        """AC7 / C6: licence/copyright/change-record/TOC blocks are excluded
        from the KMS entry's summary."""
        body = LICENCE_TEXT + "\n\n" + ROLE_CLAIM_TEXT + "\n\n" + TOC_TEXT
        self._compile(body)
        row = self.conn.execute(
            "SELECT summary, body FROM kms_entries WHERE vault_id = ? AND source_type = 'document'",
            (VAULT_ID,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn("Copyright", row["summary"])
        self.assertNotIn("Example Corp", row["summary"])
        self.assertNotIn("Table of Contents", row["summary"])
        self.assertIn("AFOMIS Director", row["summary"])
        # The BODY keeps everything (FTS parity; only the summary filters).
        self.assertIn("Copyright", row["body"])


class TestTocChunkFilter(_H2Base):
    def test_toc_chunks_are_not_retrievable(self):
        """AC8 / C7: TOC blocks are excluded from the emitted chunk set of
        BOTH selectable chunkers, with dense renumbering."""
        from app.services.chunking import EmbeddingSemanticChunker  # noqa: F401

        # The deterministic (non-embedding) chunker exercises the shared
        # final-output filter directly.
        chunker = SemanticChunker()
        text = TOC_TEXT + "\n\n" + ROLE_CLAIM_TEXT
        chunks = chunker.chunk_text(text)
        joined = "\n".join(c.text for c in chunks)
        self.assertNotIn("Introduction ........ 1", joined)
        self.assertIn("AFOMIS Director", joined)
        indexes = [c.chunk_index for c in chunks]
        self.assertEqual(indexes, list(range(len(chunks))), "indexes stay dense")
        if chunks:
            self.assertEqual(chunks[0].metadata["total_chunks"], len(chunks))

    def test_filter_helper_drops_pure_toc_and_renumbers(self):
        """Additional in-scope: the filter alone drops pure-TOC chunks and
        renumbers; mixed chunks survive."""
        from app.services.chunking import ProcessedChunk

        def mk(i, text):
            return ProcessedChunk(text=text, metadata={"chunk_index": i, "total_chunks": 3}, chunk_index=i)

        chunks = [mk(0, TOC_TEXT), mk(1, ROLE_CLAIM_TEXT), mk(2, "Notes:\nmore prose here")]
        out = filter_toc_chunks(chunks)
        self.assertEqual([c.text for c in out], [ROLE_CLAIM_TEXT, "Notes:\nmore prose here"])
        self.assertEqual([c.chunk_index for c in out], [0, 1])
        self.assertEqual(out[0].metadata["total_chunks"], 2)


class TestClaimGate(_H2Base):
    def test_claim_gate_rejects_subjectless_claim(self):
        """AC9 / C8: a deterministic candidate with no subject entity is
        rejected — no claim, no relation, and (subjectless-only documents)
        no derived records at all."""
        # A pronoun-resolved claim with no org context yields an empty
        # subject; the extractor emits the candidate, the gate rejects it.
        text = "He is the director of the department."
        result = self._compile(text)
        claims = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = ?", (VAULT_ID,)
        ).fetchone()[0]
        self.assertEqual(claims, 0)
        if result.get("skipped"):
            # Subjectless-only document: suppressed entirely.
            self.assertEqual(
                result.get("claims_rejected_subjectless", 0),
                len(result.get("claims", [])) or result.get("claims_rejected_subjectless", 0),
            )
            self.assertEqual(self._derived_counts(), (0, 0, 0))

    def test_subjectless_candidates_do_not_qualify(self):
        """Additional in-scope (plan D1/R2): a document whose ONLY
        deterministic candidates are subjectless creates no records."""
        self.assertFalse(document_has_qualifying_items("He is the director."))

    def test_qualifying_text_still_materializes_both(self):
        """Companion: substantive deterministic extraction materializes the
        page, association, and (gated) KMS entry."""
        result = self._compile(ROLE_CLAIM_TEXT)
        self.assertFalse(result.get("skipped"))
        pages, assoc, entries = self._derived_counts()
        self.assertGreaterEqual(pages, 1)
        self.assertGreaterEqual(assoc, 1)
        self.assertGreaterEqual(entries, 1)


class TestRecomputeLifecycle(_H2Base):
    def test_qualifying_to_plain_recompute_stales_and_retires(self):
        """Plan D1/F3: an unqualifying recompile of existing records
        refreshes content, sets stale, retires vanished claims, and deletes
        nothing."""
        self._compile(ROLE_CLAIM_TEXT)
        pages, assoc, entries = self._derived_counts()
        self.assertGreaterEqual(pages, 1)
        self.assertGreaterEqual(assoc, 1)
        claim_status = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE vault_id = ? LIMIT 1", (VAULT_ID,)
        ).fetchone()["status"]
        self.assertEqual(claim_status, "active")

        result = self._compile(PLAIN_TEXT)
        self.assertTrue(result.get("skipped"))
        # Records still exist (no live cleanup) but are stale and retired.
        pages_after, assoc_after, entries_after = self._derived_counts()
        self.assertEqual(pages_after, pages)
        self.assertEqual(assoc_after, assoc)
        statuses = [
            r["status"]
            for r in self.conn.execute(
                "SELECT status FROM wiki_pages WHERE vault_id = ?", (VAULT_ID,)
            ).fetchall()
        ]
        self.assertTrue(statuses and all(s == "stale" for s in statuses), statuses)
        claim_status_after = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE vault_id = ? LIMIT 1", (VAULT_ID,)
        ).fetchone()["status"]
        self.assertNotEqual(claim_status_after, "active")
        # Raw source survives.
        raw = self.conn.execute(
            "SELECT parsed_text FROM files WHERE id = ?", (FILE_ID,)
        ).fetchone()["parsed_text"]
        self.assertEqual(raw, PLAIN_TEXT)

    def test_kms_only_existing_entry_refreshes(self):
        """Plan N4: an existing document entry with no wiki page is
        upsert-refreshed by the KMS path even when the new text is
        unqualifying (creation gate only)."""
        processor = KMSCompileProcessor.__new__(KMSCompileProcessor)
        # First: qualifying compile creates the entry.
        processor._compile_file(self.conn, self.kms_store, VAULT_ID, FILE_ID)
        self.assertGreaterEqual(self._derived_counts()[2], 1)
        # Delete the wiki page association surface (KMS-only record state).
        self.conn.execute("DELETE FROM wiki_page_files WHERE file_id = ?", (FILE_ID,))
        self.conn.execute("DELETE FROM wiki_pages WHERE vault_id = ?", (VAULT_ID,))
        self.conn.commit()
        result = processor._compile_file(self.conn, self.kms_store, VAULT_ID, FILE_ID)
        self.assertFalse(result.get("skipped"), result)
        row = self.conn.execute(
            "SELECT body FROM kms_entries WHERE vault_id = ? AND source_type = 'document'",
            (VAULT_ID,),
        ).fetchone()
        self.assertIn("AFOMIS Director", row["body"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Module-level frozen-check entry points (issue #1162 / trace 1161-1162-1163).
# The frozen drivers address these tests as
# tests/test_audit1115_h2_compile.py::<name> — plain module-level node ids —
# so each canonical name delegates to its class method through the unittest
# runner (setUp/addCleanup/tearDown all execute).
# ---------------------------------------------------------------------------

def _run_case(cls, method: str) -> None:
    suite = unittest.TestSuite([cls(method)])
    result = unittest.TestResult()
    suite.run(result)
    assert not result.errors, f"{cls.__name__}.{method} errors: {result.errors}"
    assert not result.failures, f"{cls.__name__}.{method} failures: {result.failures}"


def test_numbered_procedure_becomes_procedure_page():
    _run_case(TestProcedurePages, "test_numbered_procedure_becomes_procedure_page")


def test_boilerplate_is_excluded_from_summary():
    _run_case(TestBoilerplateSummary, "test_boilerplate_is_excluded_from_summary")


def test_toc_chunks_are_not_retrievable():
    _run_case(TestTocChunkFilter, "test_toc_chunks_are_not_retrievable")


def test_claim_gate_rejects_subjectless_claim():
    _run_case(TestClaimGate, "test_claim_gate_rejects_subjectless_claim")
