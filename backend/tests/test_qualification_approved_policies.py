"""Approved extraction-policy qualification overlay (issue #1162 AC2, trace
1161-1162-1163-wiki-compiler-review-queue, check C4).

Frozen arm's-length check authored BEFORE any fix exists. The approved owner
decision (November 18, 2026) splits successful-but-unqualifying compiles from
failures and from substantive output:

- successful extraction with ZERO qualifying deterministic items and ZERO
  enabled-curator output derives NO wiki page, NO wiki-page file association
  and NO document-sourced KMS entry, while ``files.parsed_text`` survives
  unchanged (the suppression test below; RED at base, GREEN after the fix —
  the base tree always materializes one page, one association and one entry
  for ordinary prose);
- substantive deterministic extraction still materializes a page plus its
  file association (companion, green at both ends);
- an enabled curator reporting accepted output still materializes a page
  (companion, green at both ends);
- a genuine compile failure path stays a failure, never a silent skip
  (companion, green at both ends).
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models.database import run_migrations
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.kms_store import KMSStore
from app.services.wiki_compiler import (
    WikiCompiler,
    extract_entities_from_text,
)
from app.services.wiki_store import WikiStore

# Ordinary prose: no acronyms ("X stands for Y"), no ALL-CAPS-org role claims
# ("Person is the ORG Role"), no possessive-org or pronoun-role patterns. The
# deterministic extractor returns an empty result for it (asserted in the
# suppression test so the premise cannot silently drift).
PLAIN_WEATHER_TEXT = (
    "The morning began overcast and mild across the valley. "
    "A light rain fell through the middle of the day and gathered into "
    "small puddles along the garden path. "
    "By early evening the clouds had begun to break and a quiet sunset "
    "settled over the fields."
)

# Substantive deterministic text: one ALL-CAPS-org role claim.
ROLE_CLAIM_TEXT = "Alice Smith is the AFOMIS Director."

VAULT_ID = 1
FILE_ID = 1


class TestQualificationApprovedPolicies(unittest.TestCase):
    """Hermetic overlay: temp DB + run_migrations, one vault, one file row."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        db_path = str(Path(self._td) / "test.db")
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO files "
            "(id, vault_id, file_path, file_name, file_size, status, parsed_text) "
            "VALUES (1, 1, '/tmp/weather.txt', 'weather.txt', 100, 'indexed', ?)",
            (PLAIN_WEATHER_TEXT,),
        )
        self.conn.commit()
        self.store = WikiStore(self.conn)
        self.compiler = WikiCompiler(self.conn, self.store)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._td, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def _derived_counts(self):
        pages = self.conn.execute(
            "SELECT COUNT(*) AS n FROM wiki_pages WHERE vault_id = ?",
            (VAULT_ID,),
        ).fetchone()["n"]
        associations = self.conn.execute(
            "SELECT COUNT(*) AS n FROM wiki_page_files "
            "WHERE vault_id = ? AND file_id = ?",
            (VAULT_ID, FILE_ID),
        ).fetchone()["n"]
        kms_document_entries = self.conn.execute(
            "SELECT COUNT(*) AS n FROM kms_entries "
            "WHERE vault_id = ? AND source_type = 'document'",
            (VAULT_ID,),
        ).fetchone()["n"]
        return pages, associations, kms_document_entries

    def _parsed_text(self):
        row = self.conn.execute(
            "SELECT parsed_text FROM files WHERE id = ?", (FILE_ID,)
        ).fetchone()
        return dict(row)["parsed_text"]

    def _probe_summary(self):
        pages, associations, entries = self._derived_counts()
        return (
            "approved-policy probe: derived=(wiki_pages=%d, associations=%d, "
            "kms_document_entries=%d) expected=(0,0,0)"
            % (pages, associations, entries)
        )

    # -- the frozen DISCRIMINATING case (#1162 AC2) -----------------------

    def test_successful_empty_extraction_derives_no_records(self):
        """Zero qualifying output: no page, no association, no KMS document
        entry, and files.parsed_text unchanged. RED at base, GREEN after the
        approved suppression policy lands."""
        # Premise guard: the probe text must yield an empty extraction, or
        # this check would be measuring the wrong thing.
        extraction = extract_entities_from_text(PLAIN_WEATHER_TEXT)
        self.assertEqual(extraction.acronyms, [])
        self.assertEqual(extraction.role_claims, [])
        self.assertEqual(extraction.persons, [])

        original_parsed_text = self._parsed_text()
        self.assertEqual(original_parsed_text, PLAIN_WEATHER_TEXT)

        with patch.object(settings, "wiki_llm_curator_enabled", False):
            self.assertFalse(
                settings.wiki_llm_curator_enabled,
                "approved-policy probe: curator must be disabled for the "
                "zero-qualifying-output scenario",
            )
            # Wiki ingest compile, exactly as the ingest enqueue drives it
            # (file_id in input_json, no inline text -> durable parsed_text).
            self.compiler.compile_ingest_job(
                vault_id=VAULT_ID,
                input_json={"file_id": FILE_ID, "vault_id": VAULT_ID},
            )
            # KMS document compile: the per-file path the KMS worker
            # dispatches for an ingest job.
            kms_store = KMSStore(self.conn)
            KMSCompileProcessor._compile_file(
                self.conn, kms_store, VAULT_ID, FILE_ID
            )

        summary = self._probe_summary()
        print(summary)
        pages, associations, entries = self._derived_counts()
        self.assertEqual(pages, 0, summary)
        self.assertEqual(associations, 0, summary)
        self.assertEqual(entries, 0, summary)
        self.assertEqual(
            self._parsed_text(),
            PLAIN_WEATHER_TEXT,
            summary + " — raw source (files.parsed_text) must survive "
            "unchanged when derived records are suppressed",
        )

    # -- mandated companion cases (#1162 issue body) -----------------------

    def test_curator_substantive_output_still_materializes(self):
        """An enabled curator reporting accepted output is substantive: the
        compile must still materialize a wiki page for the file."""
        curator_summary = {
            "errors": [],
            "accepted": 1,
            "rejected": 0,
            "lint": 0,
            "calls": 1,
            "input_chars": len(PLAIN_WEATHER_TEXT),
        }
        with patch.object(
            settings, "wiki_llm_curator_enabled", True
        ), patch.object(
            WikiCompiler,
            "_maybe_run_curator",
            return_value=curator_summary,
        ):
            self.compiler.compile_ingest_job(
                vault_id=VAULT_ID,
                input_json={"file_id": FILE_ID, "vault_id": VAULT_ID},
            )
        pages, associations, _ = self._derived_counts()
        self.assertGreaterEqual(
            pages,
            1,
            "curator-substantive output must materialize a wiki page "
            "(approved-policy probe companion)",
        )
        self.assertGreaterEqual(
            associations,
            1,
            "curator-substantive output must materialize the file "
            "association (approved-policy probe companion)",
        )

    def test_extractor_failure_remains_a_failure(self):
        """A genuine compile failure (file row missing) must raise, not
        silently skip: suppression is only for successful zero-qualifying
        extraction."""
        with patch.object(settings, "wiki_llm_curator_enabled", False):
            with self.assertRaises(ValueError) as ctx:
                self.compiler.compile_ingest_job(
                    vault_id=VAULT_ID,
                    input_json={"file_id": 999, "vault_id": VAULT_ID},
                )
        self.assertIn(
            "not found",
            str(ctx.exception),
            "approved-policy probe companion: the failure path must keep "
            "raising its diagnostic error",
        )

    def test_substantive_deterministic_extraction_still_materializes(self):
        """Qualifying deterministic extraction still materializes a page plus
        its file association."""
        self.conn.execute(
            "UPDATE files SET parsed_text = ? WHERE id = ?",
            (ROLE_CLAIM_TEXT, FILE_ID),
        )
        self.conn.commit()
        extraction = extract_entities_from_text(ROLE_CLAIM_TEXT)
        self.assertGreaterEqual(
            len(extraction.role_claims),
            1,
            "approved-policy probe companion: the role-claim text must "
            "yield deterministic extraction",
        )
        with patch.object(settings, "wiki_llm_curator_enabled", False):
            self.compiler.compile_ingest_job(
                vault_id=VAULT_ID,
                input_json={"file_id": FILE_ID, "vault_id": VAULT_ID},
            )
        pages, associations, _ = self._derived_counts()
        self.assertGreaterEqual(
            pages,
            1,
            "substantive deterministic extraction must materialize a wiki "
            "page (approved-policy probe companion)",
        )
        self.assertGreaterEqual(
            associations,
            1,
            "substantive deterministic extraction must materialize the file "
            "association (approved-policy probe companion)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
