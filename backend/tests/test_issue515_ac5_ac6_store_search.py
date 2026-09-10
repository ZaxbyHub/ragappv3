"""Issue #515 acceptance tests — AC5 (SEARCH-003) and AC6 (SEARCH-004).

AC5: WikiStore.search slices the FTS id list to ``limit`` BEFORE applying
the SQL filters/sort, so a matching row inserted last (highest rowid) can be
dropped from the candidate pool and never seen by the status filter or the
title sort. Post-fix filters/order first, then cap: with 25 pages matching a
query where the ONLY status='verified' page (also the alphabetically-first
title) is inserted LAST, a limit=20 search with the status filter must
return it and a title-sorted limit=20 search must rank it first.

AC6: the wiki FTS helpers and ``KMSStore._fts_entry_ids`` pass the raw
search string to FTS5 MATCH. An ordinary hyphenated term like ``Model-X``
is rejected by the FTS5 query parser (raw `` OperationalError`` through
WikiStore, silently swallowed to [] by KMSStore). Post-fix an ordinary
hyphenated query must return the seeded records through both store search
paths, quoted-phrase queries must keep working, and a garbage FTS-syntax
query (unbalanced quote) must never raise.

Direct store-level tests, mirroring ``test_wiki_store.py``'s harness.
"""

import os
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


def _make_store():
    from app.models.database import run_migrations
    from app.services.wiki_store import WikiStore

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test Vault')")
    conn.commit()
    return WikiStore(conn), conn


class TestIssue515Ac5SearchFiltersBeforeCap(unittest.TestCase):
    """AC5 (SEARCH-003): filters/order must apply before the limit cap."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def _seed_25_pages(self):
        """24 draft pages inserted first, then the single verified page whose
        title also sorts alphabetically first — inserted LAST so its rowid is
        highest and a pre-cap slice drops it. Every page matches 'quantumwidget'."""
        for i in range(1, 25):
            self.store.create_page(
                vault_id=1,
                title=f"Zeta Report {i:02d}",
                page_type="entity",
                markdown="quantumwidget findings.",
                status="draft",
            )
        last = self.store.create_page(
            vault_id=1,
            title="Aardvark Report",
            page_type="entity",
            markdown="quantumwidget findings, verified.",
            status="verified",
        )
        return last

    def test_issue515_ac5_status_filter_finds_page_inserted_last(self):
        last_page = self._seed_25_pages()

        result = self.store.search(
            vault_id=1, query="quantumwidget", limit=20, status="verified"
        )
        pages = result["pages"]
        self.assertEqual(
            [p.id for p in pages],
            [last_page.id],
            "search with status filter and limit=20 must return the ONLY "
            "verified page even though it was inserted last (rowid 25); the "
            "SQL filter must apply before the candidate cap",
        )

    def test_issue515_ac5_title_sorted_top20_includes_alphabetically_first(self):
        self._seed_25_pages()

        result = self.store.search(
            vault_id=1, query="quantumwidget", limit=20, sort_by="title"
        )
        pages = result["pages"]
        self.assertEqual(
            len(pages), 20, f"expected the capped result size: {[p.title for p in pages]}"
        )
        self.assertEqual(
            pages[0].title,
            "Aardvark Report",
            "title-sorted search (limit=20) must include — and rank first — "
            "the alphabetically-first page inserted last; "
            f"got top titles {[p.title for p in pages[:3]]}",
        )


class TestIssue515Ac6HyphenatedSearch(unittest.TestCase):
    """AC6 (SEARCH-004): ordinary hyphenated terms on wiki AND KMS stores."""

    def setUp(self):
        self.store, self.conn = _make_store()
        self.wiki_page = self.store.create_page(
            vault_id=1,
            title="Model-X Notes",
            page_type="entity",
            markdown="Notes about the Model-X hardware revision.",
        )

        from app.services.kms_store import KMSStore

        self.kms_store = KMSStore(self.conn)
        self.kms_entry = self.kms_store.create_entry(
            vault_id=1,
            title="Model-X Runbook",
            body="The Model-X runbook covers setup and teardown.",
            summary="Model-X setup",
        )

    def tearDown(self):
        self.conn.close()

    def test_issue515_ac6_wiki_search_hyphenated_term(self):
        try:
            result = self.store.search(vault_id=1, query="Model-X")
        except sqlite3.OperationalError as exc:
            self.fail(
                "WikiStore.search must handle an ordinary hyphenated query "
                f"like 'Model-X' without leaking an FTS5 OperationalError: {exc}"
            )
        page_ids = [p.id for p in result["pages"]]
        self.assertIn(
            self.wiki_page.id,
            page_ids,
            f"wiki search for 'Model-X' must return the seeded page "
            f"'Model-X Notes'; got pages {page_ids!r}",
        )

    def test_issue515_ac6_wiki_search_quoted_phrase_still_works(self):
        result = self.store.search(vault_id=1, query='"Model-X"')
        self.assertIn(
            self.wiki_page.id,
            [p.id for p in result["pages"]],
            "a user-supplied quoted-phrase search must keep matching the "
            "seeded page",
        )

    def test_issue515_ac6_wiki_search_garbage_fts_syntax_does_not_raise(self):
        try:
            result = self.store.search(vault_id=1, query='O"Brien')
        except sqlite3.OperationalError as exc:
            self.fail(
                "WikiStore.search must not raise on a garbage FTS-syntax "
                f"query (unbalanced quote): {exc}"
            )
        # Sensible result: either nothing matched or the phrase was safely
        # quoted — never an exception.
        self.assertIsInstance(result, dict)

    def test_issue515_ac6_kms_search_hyphenated_term(self):
        entries = self.kms_store.list_entries(vault_id=1, search="Model-X")
        self.assertIn(
            self.kms_entry.id,
            [e.id for e in entries],
            f"KMSStore.list_entries(search='Model-X') must return the seeded "
            f"entry 'Model-X Runbook'; got {[e.title for e in entries]!r}",
        )

    def test_issue515_ac6_kms_search_quoted_phrase_still_works(self):
        entries = self.kms_store.list_entries(vault_id=1, search='"Model-X"')
        self.assertIn(
            self.kms_entry.id,
            [e.id for e in entries],
            "a user-supplied quoted-phrase search must keep matching the "
            "seeded KMS entry",
        )

    def test_issue515_ac6_kms_search_garbage_fts_syntax_does_not_raise(self):
        try:
            entries = self.kms_store.list_entries(vault_id=1, search='O"Brien')
        except sqlite3.OperationalError as exc:
            self.fail(
                "KMSStore.list_entries must not raise on a garbage FTS-syntax "
                f"query (unbalanced quote): {exc}"
            )
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()
