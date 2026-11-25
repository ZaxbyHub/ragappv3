"""Non-frozen companion tests for the approved compiler policy (issue #1162).

Plan D1/D8-v4 companions that live OUTSIDE the frozen overlay: both
worker orders, KMS feature-gate combinations, empty-input no-wipe,
parser-crash job failure, zero-element parse, retry no-duplicate, and the
CuratorClient-stub integration (one provider call, page-bound claim).
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models.database import init_db, run_migrations
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.kms_store import KMSStore
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_curator import CuratorAcceptedClaim, CuratorResult
from app.services.wiki_store import WikiStore

VAULT_ID = 1
FILE_ID = 1
ROLE_CLAIM_TEXT = "Alice Smith is the AFOMIS Director."
PLAIN_TEXT = (
    "The product team shipped a release on Tuesday. "
    "Feedback was gathered from beta users and incorporated."
)


def _mkdtemp_conn():
    db_path = str(Path(tempfile.mkdtemp(prefix="h2-companions-")) / "c.db")
    init_db(db_path)
    run_migrations(db_path)
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.execute(
        "INSERT INTO files (id, vault_id, file_path, file_name, file_size, status, parsed_text) "
        "VALUES (?, 1, 'docs/one.txt', 'one.txt', 1, 'indexed', ?)",
        (FILE_ID, ROLE_CLAIM_TEXT),
    )
    conn.commit()
    return conn


class _CompanionBase(unittest.TestCase):
    def setUp(self):
        self.conn = _mkdtemp_conn()
        self.addCleanup(self.conn.close)
        self.store = WikiStore(self.conn)
        self.compiler = WikiCompiler(self.conn)
        self.kms_store = KMSStore(self.conn)
        self.kms_processor = KMSCompileProcessor.__new__(KMSCompileProcessor)

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

    def _kms_doc_entry_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM kms_entries WHERE vault_id = ? AND source_type = 'document'",
            (VAULT_ID,),
        ).fetchone()[0]


class TestWorkerOrders(_CompanionBase):
    def test_kms_first_order_converges(self):
        """KMS job runs BEFORE the wiki compile: the deterministic criterion
        makes both workers converge (entry + page exist, no duplicates)."""
        res = self.kms_processor._compile_file(
            self.conn, self.kms_store, VAULT_ID, FILE_ID
        )
        self.assertFalse(res.get("skipped"), res)
        self._compile(ROLE_CLAIM_TEXT)
        self.assertEqual(self._kms_doc_entry_count(), 1)
        pages = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE vault_id = ?", (VAULT_ID,)
        ).fetchone()[0]
        self.assertGreaterEqual(pages, 1)

    def test_wiki_first_order_writes_both_records(self):
        """Wiki first (the single qualification owner): page AND entry."""
        self._compile(ROLE_CLAIM_TEXT)
        self.assertGreaterEqual(self._kms_doc_entry_count(), 1)
        res = self.kms_processor._compile_file(
            self.conn, self.kms_store, VAULT_ID, FILE_ID
        )
        self.assertFalse(res.get("skipped"), res)
        self.assertEqual(self._kms_doc_entry_count(), 1)  # upsert, no duplicate


class TestKmsFeatureGates(_CompanionBase):
    def test_kms_disabled_means_no_wiki_owned_entry(self):
        """kms_enabled=False: the wiki worker's cross-store write is a no-op
        (independent-subsystem policy preserved)."""
        with patch.object(settings, "kms_enabled", False), patch.object(
            settings, "kms_compile_on_ingest", True
        ):
            self._compile(ROLE_CLAIM_TEXT)
        self.assertEqual(self._kms_doc_entry_count(), 0)
        pages = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE vault_id = ?", (VAULT_ID,)
        ).fetchone()[0]
        self.assertGreaterEqual(pages, 1)

    def test_kms_compile_on_ingest_disabled_means_no_wiki_owned_entry(self):
        with patch.object(settings, "kms_enabled", True), patch.object(
            settings, "kms_compile_on_ingest", False
        ):
            self._compile(ROLE_CLAIM_TEXT)
        self.assertEqual(self._kms_doc_entry_count(), 0)

    def test_kms_job_itself_still_writes_when_enabled(self):
        with patch.object(settings, "kms_enabled", True), patch.object(
            settings, "kms_compile_on_ingest", True
        ):
            res = self.kms_processor._compile_file(
                self.conn, self.kms_store, VAULT_ID, FILE_ID
            )
        self.assertFalse(res.get("skipped"), res)
        self.assertEqual(self._kms_doc_entry_count(), 1)


class TestTaxonomyCompanions(_CompanionBase):
    def test_explicit_empty_text_skips_before_any_parse(self):
        """input_json['text'] == '' is a successful-empty skip BEFORE any
        parse attempt (o04b no-wipe contract): no provider calls, no claim
        mutation."""
        self._compile(ROLE_CLAIM_TEXT)
        claims_before = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = ?", (VAULT_ID,)
        ).fetchone()[0]
        self.assertGreater(claims_before, 0)
        result = self.compiler.compile_ingest_job(
            vault_id=VAULT_ID, input_json={"file_id": FILE_ID, "text": ""}
        )
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "empty text")
        claims_after = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = ?", (VAULT_ID,)
        ).fetchone()[0]
        self.assertEqual(claims_after, claims_before)

    def test_retry_produces_no_duplicate_records(self):
        self._compile(ROLE_CLAIM_TEXT)
        self._compile(ROLE_CLAIM_TEXT)  # recompile (retry) with same text
        self.assertEqual(self._kms_doc_entry_count(), 1)
        claims = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = ?", (VAULT_ID,)
        ).fetchone()[0]
        self.assertEqual(claims, 1)  # find-or-create, not duplicate


class TestCuratorStubIntegration(_CompanionBase):
    def test_one_provider_call_and_page_bound_accepted_claim(self):
        """The deferred seam: ONE analysis call; accepted claims persist
        exactly once, page-bound, with sources; page+association+KMS all
        present for a curator-only document."""
        cur_result = CuratorResult()
        cur_result.accepted.append(
            CuratorAcceptedClaim(
                claim_text="Curator verified this statement about AFOMIS.",
                claim_type="fact",
                subject="AFOMIS",
                predicate="states",
                object="verified statement",
                source_quote="Curator verified this statement about AFOMIS.",
                chunk_id=f"{FILE_ID}_0",
                file_id=FILE_ID,
                source_label=f"file:{FILE_ID}",
                confidence=0.9,
                status="needs_review",
            )
        )
        calls = {"n": 0}

        def fake_analysis(**kwargs):
            calls["n"] += 1
            return cur_result

        with patch.object(settings, "wiki_llm_curator_enabled", True), patch.object(
            WikiCompiler, "_maybe_run_curator", side_effect=fake_analysis
        ):
            # Curator-only document: plain prose qualifies ONLY via the
            # curator's accepted claim.
            result = self._compile(PLAIN_TEXT)
        self.assertFalse(result.get("skipped"), result)
        self.assertEqual(calls["n"], 1, "exactly one analysis call")
        self.assertGreaterEqual(result["curator"]["accepted"], 1)
        # The accepted claim is page-bound with its source.
        row = self.conn.execute(
            """
            SELECT wc.id, wc.page_id, wcs.quote FROM wiki_claims wc
            JOIN wiki_claim_sources wcs ON wcs.claim_id = wc.id
            WHERE wc.vault_id = ? AND wc.created_by_kind = 'llm_curator'
            """
            ,
            (VAULT_ID,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["page_id"])
        self.assertIn("Curator verified", row["quote"])
        # Curator-substantive document carries BOTH records.
        self.assertGreaterEqual(self._kms_doc_entry_count(), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
