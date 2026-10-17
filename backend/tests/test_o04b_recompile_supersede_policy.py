"""Issue #794 policy battery — supersede_unrederived_file_claims edge behavior.

Non-frozen companion to test_o04_wiki_recompile_supersede.py (the frozen
acceptance file): pins the recompile-supersede policy's edge cases the frozen
checks do not reach — multi-source split + finding dedupe, quote-presence
keeps, query-claim/curator-claim preservation, fail-open, no-op guarantees.
The supersession-expecting tests are RED at base; the no-op-shaped
preservation tests are GREEN at base by design and must STAY green after the
fix. Harness mirrors test_issue515_wiki_compiler.py.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto

from app.config import settings  # noqa: E402
from app.models.database import run_migrations  # noqa: E402
from app.services.wiki_compiler import WikiCompiler  # noqa: E402
from app.services.wiki_store import WikiStore  # noqa: E402

V1 = "Alice Smith is the AFOMIS Director. Carol Doe is the AFOMIS Deputy Director."
V2 = "Bob Jones is the AFOMIS Director. Carol Doe is the AFOMIS Deputy Director."


def _make_env(vaults=(1,)):
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    for vid in vaults:
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, 'Test')", (vid,)
        )
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, file_size, status) "
        "VALUES (1, 1, ?, 'test.txt', 100, 'indexed')",
        (str(Path(td) / "test.txt"),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, file_size, status) "
        "VALUES (2, 1, ?, 'other.txt', 100, 'indexed')",
        (str(Path(td) / "other.txt"),),
    )
    conn.commit()
    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)
    return conn, store, compiler, td


class _O04bBase(unittest.TestCase):
    def setUp(self):
        self.conn, self.store, self.compiler, self._td = _make_env()
        self._snap = getattr(settings, "wiki_llm_curator_enabled")
        settings.wiki_llm_curator_enabled = False

    def tearDown(self):
        settings.wiki_llm_curator_enabled = self._snap
        self.conn.close()
        shutil.rmtree(self._td, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def _page_id_for_file(self):
        row = self.conn.execute(
            "SELECT page_id FROM wiki_page_files WHERE file_id = 1"
        ).fetchone()
        return dict(row)["page_id"] if row else None

    def _seed_claim(self, claim_text, *, quote, page_id, vault_id=1,
                    status="active", source_kind="document", file_id=1,
                    created_by_kind=None):
        claim = self.store.create_claim(
            vault_id=vault_id,
            claim_text=claim_text,
            source_type="document",
            page_id=page_id,
            status=status,
            created_by_kind=created_by_kind,
        )
        if source_kind == "document":
            self.store.attach_source(
                claim_id=claim.id, source_kind="document", file_id=file_id,
                chunk_id=f"{file_id}_0", source_label=f"file:{file_id}",
                quote=quote, confidence=0.8,
            )
        self.conn.commit()
        return claim.id

    def _claim_status(self, claim_id):
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        return dict(row)["status"] if row else None

    def _open_findings(self, finding_type, claim_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings WHERE finding_type = ? "
            "AND status = 'open' AND related_claim_ids_json = ?",
            (finding_type, f"[{claim_id}]"),
        ).fetchone()[0]


class TestMultiSourceSplit(_O04bBase):
    def test_multi_source_claim_stays_active_with_one_weak_finding(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        # Claim supported by file 1 AND file 2; file 1 drops its sentence.
        cid = self._seed_claim(
            "Zoe Quinn is the SIGMA Director.",
            quote="Zoe Quinn is the SIGMA Director.",
            page_id=page,
        )
        self.store.attach_source(
            claim_id=cid, source_kind="document", file_id=2,
            source_label="file:2", quote="Zoe Quinn is the SIGMA Director.",
            confidence=0.8,
        )
        self.conn.commit()

        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})  # repeat

        self.assertEqual(self._claim_status(cid), "active")
        self.assertEqual(self._open_findings("weak_provenance", cid), 1)


class TestSoleSourceFindingBounded(_O04bBase):
    def test_sole_source_superseded_stale_finding_not_duplicated(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        cid = self._seed_claim(
            "Zoe Quinn is the SIGMA Director.",
            quote="Zoe Quinn is the SIGMA Director.",
            page_id=page,
        )
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})  # repeat

        self.assertEqual(self._claim_status(cid), "superseded")
        self.assertEqual(self._open_findings("stale", cid), 1)


class TestQuotePresenceKeeps(_O04bBase):
    def test_extraction_skipped_sentence_still_present_is_kept(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        # Seeded directly (not extractor-derived): present verbatim in the
        # text, so the QUOTE-presence arm must keep it even though the
        # deterministic pass never re-derives this sentence.
        skipped_sentence = "He mentioned it briefly."
        cid = self._seed_claim(
            skipped_sentence, quote=skipped_sentence, page_id=page
        )
        v2_with_skip = V2 + " " + skipped_sentence
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": v2_with_skip})
        self.assertEqual(self._claim_status(cid), "active")
        self.assertEqual(self._open_findings("stale", cid), 0)

    def test_case_variant_quote_kept_via_normalized_presence(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        # The claim TEXT is deliberately NOT re-derivable: it never matches the
        # deterministic extractor and never appears verbatim in either text,
        # so keep-by-id can never fire and the QUOTE arm alone decides. The
        # quote is a case-variant of a sentence present in v2 — the
        # normalized/token-boundary presence arm must keep the claim.
        cid = self._seed_claim(
            "sigma operations are led by zoe quinn",
            quote="Zoe quinn is the SIGMA director.",
            page_id=page,
        )
        v2_variant = V2 + " Zoe Quinn is the SIGMA Director."
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": v2_variant})
        self.assertEqual(self._claim_status(cid), "active")
        # And when the variant sentence disappears, the quote is verifiably
        # gone and the claim supersedes.
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._claim_status(cid), "superseded")


class TestIdenticalRecompileNoOp(_O04bBase):
    def test_byte_identical_recompile_changes_nothing(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        # Curator-shaped claim: paraphrased claim_text, verbatim quote present.
        paraphrase = "The AFOMIS Director role is held by Alice Smith."
        cid = self._seed_claim(
            paraphrase, quote="Alice Smith is the AFOMIS Director.", page_id=page,
            created_by_kind="llm_curator",
        )
        before = {
            tuple(r) for r in self.conn.execute(
                "SELECT id, status FROM wiki_claims"
            ).fetchall()
        }
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        after = {
            tuple(r) for r in self.conn.execute(
                "SELECT id, status FROM wiki_claims"
            ).fetchall()
        }
        self.assertEqual(before, after)
        self.assertEqual(self._claim_status(cid), "active")
        total_findings = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings"
        ).fetchone()[0]
        self.assertEqual(total_findings, 0)


class TestQueryShapeUntouched(_O04bBase):
    def test_null_page_claim_with_synthetic_quote_untouched(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        # Query-synthesis shape: no page binding, quote is a synthesized
        # sentence that is NOT a span of the file text.
        cid = self._seed_claim(
            "AFOMIS leadership currently includes Alice Smith as director.",
            quote="AFOMIS leadership currently includes Alice Smith as director.",
            page_id=None,
        )
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        self.assertEqual(self._claim_status(cid), "active")
        self.assertEqual(self._open_findings("stale", cid), 0)


class TestCuratorParaphraseQuoteDriven(_O04bBase):
    def test_paraphrase_claim_superseded_only_when_quote_leaves(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        paraphrase = "The AFOMIS Director role is held by Alice Smith."
        cid = self._seed_claim(
            paraphrase, quote="Alice Smith is the AFOMIS Director.", page_id=page,
            created_by_kind="llm_curator",
        )
        # Quote still present (as part of v1) → kept.
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        self.assertEqual(self._claim_status(cid), "active")
        # Quote gone (v2 replaces Alice with Bob) → superseded.
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._claim_status(cid), "superseded")


class TestEmptyTextNoWipe(_O04bBase):
    def test_empty_text_compile_leaves_claims_untouched(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        before = {
            tuple(r) for r in self.conn.execute(
                "SELECT id, status FROM wiki_claims"
            ).fetchall()
        }
        result = self.compiler.compile_ingest_job(1, {"file_id": 1, "text": ""})
        self.assertTrue(result.get("skipped"))
        after = {
            tuple(r) for r in self.conn.execute(
                "SELECT id, status FROM wiki_claims"
            ).fetchall()
        }
        self.assertEqual(before, after)


class TestNonActiveUntouched(_O04bBase):
    def test_unverified_and_superseded_claims_not_flipped(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        unverified = self._seed_claim(
            "Zoe Quinn is the SIGMA Director.",
            quote="Zoe Quinn is the SIGMA Director.",
            page_id=page,
            status="unverified",
        )
        superseded = self._seed_claim(
            "Rosa Vega is the SIGMA Manager.",
            quote="Rosa Vega is the SIGMA Manager.",
            page_id=page,
            status="superseded",
        )
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._claim_status(unverified), "unverified")
        self.assertEqual(self._claim_status(superseded), "superseded")


class TestVaultScoping(_O04bBase):
    def test_cross_vault_claim_untouched(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        # Cross-vault corruption shape: vault 2 claim pointing at vault 1's page.
        cid = self._seed_claim(
            "Zoe Quinn is the SIGMA Director.",
            quote="Zoe Quinn is the SIGMA Director.",
            page_id=page,
            vault_id=2,
        )
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._claim_status(cid), "active")


class TestFailOpenNoQuote(_O04bBase):
    def test_empty_quote_source_fails_open(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id_for_file()
        cid = self._seed_claim(
            "Zoe Quinn is the SIGMA Director.", quote="", page_id=page
        )
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._claim_status(cid), "active")
        self.assertEqual(self._open_findings("stale", cid), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
