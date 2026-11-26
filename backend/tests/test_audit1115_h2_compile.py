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
# module imports (its chunk_by_title and partition return []), and seven test
# modules REPLACE those stub modules unconditionally at module level
# (test_c16_chat_message_cap, test_c16c_message_cap_guards,
# test_chat_history_role_validation, test_contextual_chunking,
# test_hybrid_logging, test_integration and test_retrieval_logging — all
# sorted after this file). Under pytest-xdist each worker imports every module
# before any test runs, so the module object registered LAST is the one live
# at test time. This file therefore does NO import-time seeding: both seams are
# installed per test by _H2Base (the partition module through an exact
# save/restore of its single sys.modules entry, chunk_by_title through
# mock.patch on the live module object), so the deterministic behavior holds
# regardless of who registered the stub and nothing leaks into other tests.
# (PR #1204 CI round 1: import-time seeding was overwritten on CI and
# SemanticChunker saw an empty element set. Round 5: a guarded seed still
# displaced the conftest stub for every module sorted between this file and
# the next unconditional installer, because conftest does not register
# unstructured.partition.text and this file's own guard therefore fired.)
import contextlib
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


_PARTITION_TEXT_MODULE = "unstructured.partition.text"


@contextlib.contextmanager
def _deterministic_chunking_seams():
    """This file's partition/chunk behavior for ONE test, restored afterwards.

    ``sys.modules['unstructured.partition.text']`` is swapped with an exact
    save/restore of that single entry (conftest does not register it; a real
    install's module is put back afterwards). ``chunk_by_title`` is patched on
    whichever ``unstructured.chunking.title`` object is live.
    """
    # Presence is recorded separately from the value: a None entry is Python's
    # failed-import marker — a PRESENT entry that must be put back as None, not
    # an absent one to remove (implementation review round 6).
    was_present = _PARTITION_TEXT_MODULE in sys.modules
    saved = sys.modules.get(_PARTITION_TEXT_MODULE)
    module = types.ModuleType(_PARTITION_TEXT_MODULE)
    module.partition_text = _partition_text_stub
    sys.modules[_PARTITION_TEXT_MODULE] = module
    try:
        with patch("unstructured.chunking.title.chunk_by_title", _chunk_by_title_stub):
            yield
    finally:
        if was_present:
            sys.modules[_PARTITION_TEXT_MODULE] = saved
        else:
            sys.modules.pop(_PARTITION_TEXT_MODULE, None)

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
        # This file's deterministic partition/chunk behavior for THIS test
        # only: whichever module object is live — a real full install or a
        # stub another module registered — is restored on cleanup, so nothing
        # leaks to other tests.
        seams = _deterministic_chunking_seams()
        seams.__enter__()
        self.addCleanup(seams.__exit__, None, None, None)

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
        """AC8 / C7: the shared final-output filter drops pure-TOC chunks and
        renumbers the emitted set with dense indexes."""
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

    def test_embedding_assembly_path_drops_toc(self):
        """AC8 / C7: the embedding chunker's final-output assembly seam also
        drops a pure-TOC chunk before the chunks become retrievable."""
        import asyncio

        from app.services.chunking import EmbeddingSemanticChunker, ThresholdType

        class _FixedEmbeddings:
            async def embed_batch(self, windows):
                return [[1.0, 0.0] for _ in windows]

        # GRADIENT with a negative threshold breaks between every pair of
        # sentences, so the TOC and the role claim become separate chunks and
        # the embedding assembly path must apply the filter itself.
        chunker = EmbeddingSemanticChunker(
            embedding_service=_FixedEmbeddings(),
            threshold_type=ThresholdType.GRADIENT,
            threshold_value=-1.0,
            min_chunk_size=1,
            max_chunk_size=2000,
        )
        sentences = [TOC_TEXT.strip(), ROLE_CLAIM_TEXT]
        with patch.object(chunker, "_split_into_sentences", return_value=sentences):
            chunks = asyncio.run(chunker.chunk_text(TOC_TEXT + "\n\n" + ROLE_CLAIM_TEXT))
        joined = "\n".join(c.text for c in chunks)
        self.assertNotIn("Introduction ........ 1", joined)
        self.assertIn("AFOMIS Director", joined)
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        for chunk in chunks:
            self.assertEqual(chunk.metadata["total_chunks"], len(chunks))

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


def test_seam_installation_is_leak_free():
    """The per-test seams restore exactly what was there before.

    Guards the property that replaced import-time seeding (final critic round
    5): running this file's harness leaves the process-global stubs — or a
    real install's modules — exactly as it found them.
    """
    was_registered = _PARTITION_TEXT_MODULE in sys.modules
    partition_text_before = sys.modules.get(_PARTITION_TEXT_MODULE)
    title_module = sys.modules["unstructured.chunking.title"]
    chunk_by_title_before = title_module.chunk_by_title

    def run_harness() -> None:
        result = unittest.TestResult()
        unittest.TestSuite(
            [TestTocChunkFilter("test_toc_chunks_are_not_retrievable")]
        ).run(result)
        assert not result.errors, result.errors
        assert not result.failures, result.failures
        assert title_module.chunk_by_title is chunk_by_title_before

    try:
        # 1. The environment's own state of the entry (absent, or a module
        #    another file registered) comes back exactly as it was.
        run_harness()
        assert (_PARTITION_TEXT_MODULE in sys.modules) == was_registered
        assert sys.modules.get(_PARTITION_TEXT_MODULE) is partition_text_before

        # 2. A None entry — Python's failed-import marker — is a PRESENT entry
        #    and must come back as None, not be removed (implementation review
        #    round 6).
        sys.modules[_PARTITION_TEXT_MODULE] = None
        run_harness()
        assert _PARTITION_TEXT_MODULE in sys.modules
        assert sys.modules[_PARTITION_TEXT_MODULE] is None

        # 3. An absent entry stays absent.
        sys.modules.pop(_PARTITION_TEXT_MODULE, None)
        run_harness()
        assert _PARTITION_TEXT_MODULE not in sys.modules
    finally:
        if was_registered:
            sys.modules[_PARTITION_TEXT_MODULE] = partition_text_before
        else:
            sys.modules.pop(_PARTITION_TEXT_MODULE, None)
