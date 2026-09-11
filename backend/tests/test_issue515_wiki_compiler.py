"""Issue #515 acceptance tests — WikiCompiler defects (WIKI-006/007/008).

Regression tests authored from the defect report; implementations of the
fixes do NOT exist yet. Every ``test_issue515_acN_*`` function encodes the
REQUIRED (post-fix) behavior:

  AC7  (WIKI-006) Recompiling an unchanged stale memory claim reactivates it.
  AC8  (WIKI-007) Recompiling an existing document page refreshes
                  compiler-owned content.
  AC9  (WIKI-008) Normalized-equivalent claim reuse must not duplicate sources.

AC10 (WIKI-009) lives in test_issue515_wiki_documents.py.

The fixtures mirror backend/tests/test_wiki_compile_processor.py
(``_make_env``): a real SQLite DB built by ``run_migrations`` with the
optional heavy deps stubbed the same way. No network / no LLM — the curator
is force-disabled so only the deterministic compiler paths run.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (same as test_wiki_curator.py /
# test_wiki_compile_processor.py so collection never depends on heavy deps).
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
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

from app.config import settings
from app.models.database import run_migrations
from app.services.wiki_compiler import WikiCompiler, extract_entities_from_text
from app.services.wiki_store import WikiStore

# Distinctive role-claim sentences used across the tests.
S_AFOMIS_CHIEF = "Justice Sakyi is the AFOMIS Chief."
S_AFOMIS_CHIEF_BANG = "Justice Sakyi is the AFOMIS Chief!"
S_SIGMA_DIRECTOR = "Maria Chen is the SIGMA Director."
S_SIGMA_MANAGER = "Rosa Vega is the SIGMA Manager."


def _make_env():
    """Real temp SQLite DB + WikiStore + WikiCompiler (mirrors the
    test_wiki_compile_processor._make_env harness)."""
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
    return conn, store, compiler, td


class _Issue515CompilerTestBase(unittest.TestCase):
    """Base env + curator force-disabled so deterministic paths are isolated."""

    def setUp(self):
        self.conn, self.store, self.compiler, self._td = _make_env()
        self._snap = getattr(settings, "wiki_llm_curator_enabled")
        settings.wiki_llm_curator_enabled = False

    def tearDown(self):
        settings.wiki_llm_curator_enabled = self._snap
        self.conn.close()
        import shutil
        shutil.rmtree(self._td, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def _claim_row(self, claim_id):
        row = self.conn.execute(
            "SELECT id, claim_text, status, page_id FROM wiki_claims WHERE id = ?",
            (claim_id,),
        ).fetchone()
        return dict(row) if row else None

    def _claim_id_by_text(self, text):
        row = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = ?", (text,)
        ).fetchone()
        return dict(row)["id"] if row else None


# ---------------------------------------------------------------------------
# AC7 (WIKI-006) — recompile of an unchanged stale memory claim reactivates it
# ---------------------------------------------------------------------------


class TestIssue515Ac7StaleMemoryClaimReactivation(_Issue515CompilerTestBase):

    def _seed_memory(self, memory_id, content):
        self.conn.execute(
            "INSERT OR REPLACE INTO memories (id, vault_id, content) VALUES (?, 1, ?)",
            (memory_id, content),
        )
        self.conn.commit()

    def test_issue515_ac7_recompile_reactivates_unchanged_stale_claim(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor

        # --- Part 1 (DISCRIMINATING): unchanged claim is reactivated. -------
        self._seed_memory(1, S_AFOMIS_CHIEF)
        self.compiler.promote_memory(memory_id=1, vault_id=1, is_admin=True)
        claim_id = self._claim_id_by_text(S_AFOMIS_CHIEF)
        self.assertIsNotNone(claim_id, "promotion must create the claim")
        self.assertEqual(self._claim_row(claim_id)["status"], "active")

        # Edit the memory with UNRELATED extra text (the supported claim
        # sentence itself is unchanged) -> sole-source claim goes superseded.
        self.conn.execute(
            "UPDATE memories SET content = ? WHERE id = 1",
            (S_AFOMIS_CHIEF + " An unrelated scheduling note follows.",),
        )
        self.conn.commit()
        self.store.mark_claims_stale_by_memory(memory_id=1, vault_id=1)
        self.conn.commit()
        self.assertEqual(
            self._claim_row(claim_id)["status"], "superseded",
            "stale marking must set the sole-source claim superseded (pre-state)",
        )

        # Recompile via the reindex path (re-runs promote_memory).
        job = SimpleNamespace(vault_id=1, trigger_type="settings_reindex")
        WikiCompileProcessor._handle_reindex(job, self.store, self.compiler, {})

        refreshed = self.store.get_claim(claim_id)
        self.assertEqual(
            refreshed.status,
            "active",
            "AC7: after recompile, an unchanged supported claim must be "
            "'active' again (still re-derivable from the edited memory), "
            f"got {refreshed.status!r}",
        )

        # --- Part 2 (repair-shape guard): a claim whose CONTENT changed is
        # NOT blindly revived. The new content produces its own claim; the
        # old one stays superseded. ----------------------------------------
        self._seed_memory(2, S_SIGMA_DIRECTOR)
        self.compiler.promote_memory(memory_id=2, vault_id=1, is_admin=True)
        old_claim_id = self._claim_id_by_text(S_SIGMA_DIRECTOR)
        self.assertIsNotNone(old_claim_id)
        self.assertEqual(self._claim_row(old_claim_id)["status"], "active")

        # Replace the claim sentence entirely (content actually changed).
        self.conn.execute(
            "UPDATE memories SET content = ? WHERE id = 2",
            (S_SIGMA_MANAGER,),
        )
        self.conn.commit()
        self.store.mark_claims_stale_by_memory(memory_id=2, vault_id=1)
        self.conn.commit()
        self.assertEqual(self._claim_row(old_claim_id)["status"], "superseded")

        WikiCompileProcessor._handle_reindex(job, self.store, self.compiler, {})

        # The old (changed-away) claim must NOT be blindly revived...
        self.assertEqual(
            self._claim_row(old_claim_id)["status"],
            "superseded",
            "AC7 guard: a claim whose supporting content changed must stay "
            "superseded — the repair must not blanket-reactivate stale claims",
        )
        # ...the new sentence produces its OWN active claim (distinct row).
        new_claim_id = self._claim_id_by_text(S_SIGMA_MANAGER)
        self.assertIsNotNone(
            new_claim_id,
            "AC7 guard: the replacement sentence must produce its own claim",
        )
        self.assertNotEqual(new_claim_id, old_claim_id)
        self.assertEqual(self._claim_row(new_claim_id)["status"], "active")


# ---------------------------------------------------------------------------
# AC8 (WIKI-007) — recompile refreshes compiler-owned page content
# ---------------------------------------------------------------------------


class TestIssue515Ac8DocumentPageRefresh(_Issue515CompilerTestBase):

    def test_issue515_ac8_recompile_refreshes_page_content(self):
        # POLICY LIMIT: the wiki_pages schema has no manually_edited /
        # last_manual_edit column (checked in app/models/database.py), so the
        # store cannot distinguish compiler-owned pages from manually edited
        # ones. Per the AC, this test therefore asserts ONLY the refresh
        # behavior for pages that were never manually edited. If a manual-edit
        # flag is added later, extend this test so a manually-edited page is
        # NOT clobbered by a recompile.
        text_x = (
            "The onboarding guide describes the ZEBRA badge protocol for "
            "new analysts arriving on site."
        )
        text_y = (
            "The onboarding guide describes the YAK badge protocol for "
            "remote analysts working from home."
        )

        r1 = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": text_x}
        )
        self.assertFalse(r1.get("skipped"))
        page_id = r1["page"]["id"]
        page = self.store.get_page(page_id)
        self.assertIn("ZEBRA", page.markdown, "initial compile stores content X")

        # Source text changed to Y; recompile (same file, updated text).
        r2 = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": text_y}
        )
        self.assertFalse(r2.get("skipped"))
        self.assertEqual(r2["page"]["id"], page_id, "same document keeps its page")

        page = self.store.get_page(page_id)
        self.assertIn(
            "YAK", page.markdown,
            "AC8: recompile must refresh the page markdown to the new source text",
        )
        self.assertNotIn(
            "ZEBRA", page.markdown,
            "AC8: stale content from the previous source version must not remain "
            "in compiler-owned markdown",
        )
        # Summary is compiler-owned surface too: whatever it holds, it must not
        # still reference the superseded source version.
        self.assertNotIn(
            "ZEBRA", page.summary or "",
            "AC8: a stale summary referencing the previous source must not survive "
            "a recompile",
        )


# ---------------------------------------------------------------------------
# AC9 (WIKI-008) — normalized-equivalent claim reuse must not duplicate sources
# ---------------------------------------------------------------------------


class TestIssue515Ac9NormalizedClaimReuse(_Issue515CompilerTestBase):

    def test_issue515_ac9_variant_sentence_reuse_single_source(self):
        # Job 1: a query answer citing document file 1 with sentence S.
        ext1 = extract_entities_from_text(S_AFOMIS_CHIEF)
        self.assertEqual(len(ext1.role_claims), 1)
        sentence_1 = ext1.role_claims[0]["sentence"]
        result_1 = self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": S_AFOMIS_CHIEF,
                "per_claim_sources": {
                    sentence_1: [
                        {
                            "source_kind": "document",
                            "source_label": "S1",
                            "file_id": 1,
                        }
                    ]
                },
            },
        )
        self.assertEqual(len(result_1["claims"]), 1)
        self.assertEqual(result_1["claims"][0]["status"], "active")

        # Job 2: the SAME sentence, differing only in punctuation, citing the
        # SAME document.
        ext2 = extract_entities_from_text(S_AFOMIS_CHIEF_BANG)
        self.assertEqual(len(ext2.role_claims), 1)
        sentence_2 = ext2.role_claims[0]["sentence"]
        self.assertNotEqual(sentence_1, sentence_2, "variant must differ exactly")
        from app.services.wiki_store import normalize_claim_text
        self.assertEqual(
            normalize_claim_text(sentence_1),
            normalize_claim_text(sentence_2),
            "variant must be normalized-equivalent",
        )

        self.compiler.compile_query_job(
            vault_id=1,
            input_json={
                "assistant_answer": S_AFOMIS_CHIEF_BANG,
                "per_claim_sources": {
                    sentence_2: [
                        {
                            "source_kind": "document",
                            "source_label": "S1",
                            "file_id": 1,
                        }
                    ]
                },
            },
        )

        claim_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()[0]
        self.assertEqual(
            1, claim_count,
            "AC9: a punctuation-variant sentence must reuse the existing claim",
        )
        claim_id = self._claim_id_by_text(sentence_1)
        source_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claim_sources WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()[0]
        self.assertEqual(
            1, source_count,
            "AC9: citing the same document via a normalized-equivalent sentence "
            "must not attach a duplicate source row",
        )

    def test_issue515_ac9_exact_repeat_preserving(self):
        """PRESERVING: an exact repeat of the same cited query keeps exactly
        one claim and one source row (pins today's correct behavior)."""
        ext = extract_entities_from_text(S_AFOMIS_CHIEF)
        sentence = ext.role_claims[0]["sentence"]
        inp = {
            "assistant_answer": S_AFOMIS_CHIEF,
            "per_claim_sources": {
                sentence: [
                    {"source_kind": "document", "source_label": "S1", "file_id": 1}
                ]
            },
        }
        self.compiler.compile_query_job(vault_id=1, input_json=inp)
        self.compiler.compile_query_job(vault_id=1, input_json=inp)

        claim_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()[0]
        self.assertEqual(1, claim_count)
        claim_id = self._claim_id_by_text(sentence)
        source_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claim_sources WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()[0]
        self.assertEqual(1, source_count)


if __name__ == "__main__":
    unittest.main()
