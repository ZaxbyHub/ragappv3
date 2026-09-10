"""Issue #515 acceptance tests — WikiCompiler document-page defect (WIKI-009).

  AC10 (WIKI-009) Two same-named documents get distinct pages. Today the
                   document page slug is normalize_slug("document/" +
                   file_name[:60]) which ignores the file_id, so two
                   DIFFERENT files sharing a file_name in one vault collide
                   on one wiki page.

Fixtures mirror backend/tests/test_wiki_compile_processor.py
(``_make_env``): a real SQLite DB built by ``run_migrations`` with the
optional heavy deps stubbed. Curator force-disabled — deterministic only.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (same as the other wiki test modules).
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
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_store import WikiStore

S_ZEBRA_CHIEF = "Justice Sakyi is the ZEBRA Chief."
S_YAK_DIRECTOR = "Maria Chen is the YAK Director."


class TestIssue515Ac10SameNamedDocuments(unittest.TestCase):
    """Two different file_ids sharing one file_name must not share a page.

    Assertions cover distinctness + correct association (not the slug text —
    the fix may append a stable suffix to disambiguate)."""

    def setUp(self):
        td = tempfile.mkdtemp()
        self._td = td
        db_path = str(Path(td) / "test.db")
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')"
        )
        # Two DIFFERENT files, SAME file_name, one vault.
        self.conn.execute(
            "INSERT OR REPLACE INTO files (id, vault_id, file_path, file_name, file_size, status) "
            "VALUES (1, 1, '/tmp/notes-a.txt', 'notes.txt', 100, 'indexed'), "
            "(2, 1, '/tmp/notes-b.txt', 'notes.txt', 120, 'indexed')"
        )
        self.conn.commit()
        self.store = WikiStore(self.conn)
        self.compiler = WikiCompiler(self.conn, self.store)
        self._snap = getattr(settings, "wiki_llm_curator_enabled")
        settings.wiki_llm_curator_enabled = False

    def tearDown(self):
        settings.wiki_llm_curator_enabled = self._snap
        self.conn.close()
        import shutil
        shutil.rmtree(self._td, ignore_errors=True)

    def _claim_row(self, claim_text):
        row = self.conn.execute(
            "SELECT id, claim_text, status, page_id FROM wiki_claims WHERE claim_text = ?",
            (claim_text,),
        ).fetchone()
        return dict(row) if row else None

    def test_issue515_ac10_same_file_name_distinct_pages(self):
        r1 = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 1, "text": S_ZEBRA_CHIEF}
        )
        r2 = self.compiler.compile_ingest_job(
            vault_id=1, input_json={"file_id": 2, "text": S_YAK_DIRECTOR}
        )
        self.assertFalse(r1.get("skipped"))
        self.assertFalse(r2.get("skipped"))

        page_id_1 = r1["page"]["id"]
        page_id_2 = r2["page"]["id"]
        self.assertNotEqual(
            page_id_1, page_id_2,
            "AC10: two same-named documents must get distinct wiki pages "
            "(slug derived only from file_name collides today)",
        )

        # Exactly two document pages exist for the vault.
        doc_pages = self.conn.execute(
            "SELECT id, slug FROM wiki_pages WHERE vault_id = 1 AND slug LIKE 'document%'"
        ).fetchall()
        self.assertEqual(
            2, len(doc_pages),
            f"AC10: expected 2 document pages, got {[dict(r) for r in doc_pages]}",
        )

        # Each page's compiler-owned content matches its own document.
        page_1 = self.store.get_page(page_id_1)
        page_2 = self.store.get_page(page_id_2)
        self.assertIn("ZEBRA", page_1.markdown, "page of file 1 carries file 1's text")
        self.assertIn("YAK", page_2.markdown, "page of file 2 carries file 2's text")
        self.assertNotIn("YAK", page_1.markdown)
        self.assertNotIn("ZEBRA", page_2.markdown)

        # Each document's claims live on its own page.
        claim_1 = self._claim_row(S_ZEBRA_CHIEF)
        claim_2 = self._claim_row(S_YAK_DIRECTOR)
        self.assertIsNotNone(claim_1)
        self.assertIsNotNone(claim_2)
        self.assertEqual(
            claim_1["page_id"], page_id_1,
            "claims extracted from file 1 must be associated with file 1's page",
        )
        self.assertEqual(
            claim_2["page_id"], page_id_2,
            "claims extracted from file 2 must be associated with file 2's page",
        )


if __name__ == "__main__":
    unittest.main()
