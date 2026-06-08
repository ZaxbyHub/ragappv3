"""Tests for WikiStore CRUD, search, and idempotent migrations."""

import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestWikiMigration(unittest.TestCase):
    """Database migration creates all wiki tables and indexes idempotently."""

    def _make_db(self) -> str:
        td = tempfile.mkdtemp()
        return str(Path(td) / "test.db")

    def test_all_wiki_tables_created(self):
        from app.models.database import run_migrations

        db_path = self._make_db()
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        expected = {
            "wiki_pages", "wiki_entities", "wiki_claims",
            "wiki_claim_sources", "wiki_relations",
            "wiki_compile_jobs", "wiki_lint_findings",
        }
        self.assertTrue(expected.issubset(tables), f"Missing tables: {expected - tables}")

    def test_fts_tables_created(self):
        from app.models.database import run_migrations

        db_path = self._make_db()
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        for fts in ("wiki_pages_fts", "wiki_claims_fts", "wiki_entities_fts"):
            self.assertIn(fts, tables)

    def test_idempotent_double_run(self):
        from app.models.database import run_migrations

        db_path = self._make_db()
        run_migrations(db_path)
        run_migrations(db_path)  # must not raise

    def test_indexes_created(self):
        from app.models.database import run_migrations

        db_path = self._make_db()
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        expected_indexes = {
            "idx_wiki_pages_vault_type_status",
            "idx_wiki_pages_vault_slug",
            "idx_wiki_entities_vault_name",
            "idx_wiki_claims_vault_page_status",
            "idx_wiki_claim_sources_claim_id",
            "idx_wiki_compile_jobs_vault_status",
            "idx_wiki_lint_findings_vault_status_severity",
        }
        self.assertTrue(
            expected_indexes.issubset(indexes),
            f"Missing indexes: {expected_indexes - indexes}",
        )


def _make_store():
    """Create an in-memory SQLite connection with wiki schema and return WikiStore."""
    from app.models.database import run_migrations
    from app.services.wiki_store import WikiStore

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    # Insert default vault
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test Vault')")
    conn.commit()
    return WikiStore(conn), conn


class TestWikiStorePageCRUD(unittest.TestCase):

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_create_and_get_page(self):
        page = self.store.create_page(vault_id=1, title="AFOMIS Overview", page_type="overview")
        self.assertEqual(page.title, "AFOMIS Overview")
        self.assertEqual(page.slug, "afomis-overview")
        self.assertEqual(page.status, "draft")

        fetched = self.store.get_page(page.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, page.id)

    def test_create_slug_normalized(self):
        page = self.store.create_page(vault_id=1, title="Air Force Systems!", page_type="system")
        self.assertNotIn("!", page.slug)
        self.assertIn("air-force-systems", page.slug)

    def test_list_pages(self):
        self.store.create_page(vault_id=1, title="Page A", page_type="entity")
        self.store.create_page(vault_id=1, title="Page B", page_type="acronym")
        pages = self.store.list_pages(vault_id=1)
        self.assertEqual(len(pages), 2)

    def test_list_pages_filter_by_type(self):
        self.store.create_page(vault_id=1, title="Page A", page_type="entity")
        self.store.create_page(vault_id=1, title="Page B", page_type="acronym")
        pages = self.store.list_pages(vault_id=1, page_type="acronym")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].page_type, "acronym")

    def test_update_page(self):
        page = self.store.create_page(vault_id=1, title="Old Title", page_type="entity")
        updated = self.store.update_page(page.id, vault_id=1, title="New Title", status="verified")
        self.assertEqual(updated.title, "New Title")
        self.assertEqual(updated.status, "verified")

    def test_delete_page_nullifies_claim_page_id(self):
        page = self.store.create_page(vault_id=1, title="To Delete", page_type="entity")
        claim = self.store.create_claim(
            vault_id=1, claim_text="Test claim", source_type="manual", page_id=page.id
        )
        self.store.delete_page(page.id, vault_id=1)
        # Claim still exists with page_id = NULL (ON DELETE SET NULL)
        fetched_claim = self.store.get_claim(claim.id)
        self.assertIsNotNone(fetched_claim)
        self.assertIsNone(fetched_claim.page_id)

    def test_get_nonexistent_page(self):
        result = self.store.get_page(99999)
        self.assertIsNone(result)


class TestWikiStoreClaimCRUD(unittest.TestCase):

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_create_claim_with_source(self):
        page = self.store.create_page(vault_id=1, title="Test", page_type="entity")
        claim = self.store.create_claim(
            vault_id=1,
            claim_text="Justice Sakyi is the AFOMIS Chief",
            source_type="memory",
            page_id=page.id,
            subject="Justice Sakyi",
            predicate="chief",
            object="AFOMIS",
        )
        # Attach a provenance source
        self.store.attach_source(
            claim_id=claim.id,
            source_kind="memory",
            memory_id=42,
            quote="Justice Sakyi is the AFOMIS Chief",
        )
        self.conn.commit()

        fetched = self.store.get_claim(claim.id)
        self.assertEqual(fetched.claim_text, "Justice Sakyi is the AFOMIS Chief")
        self.assertEqual(len(fetched.sources), 1)
        self.assertEqual(fetched.sources[0].memory_id, 42)

    def test_claim_cascades_sources_on_delete(self):
        claim = self.store.create_claim(
            vault_id=1, claim_text="Test", source_type="manual"
        )
        self.store.attach_source(claim_id=claim.id, source_kind="manual")
        self.conn.commit()

        # Delete the claim — sources should cascade
        self.store.delete_claim(claim.id, vault_id=1)
        rows = self.conn.execute(
            "SELECT * FROM wiki_claim_sources WHERE claim_id = ?", (claim.id,)
        ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_list_claims_by_page(self):
        page = self.store.create_page(vault_id=1, title="P", page_type="entity")
        self.store.create_claim(vault_id=1, claim_text="A", source_type="manual", page_id=page.id)
        self.store.create_claim(vault_id=1, claim_text="B", source_type="manual")
        claims = self.store.list_claims(vault_id=1, page_id=page.id)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].claim_text, "A")

    def test_update_claim(self):
        claim = self.store.create_claim(
            vault_id=1, claim_text="Original", source_type="manual"
        )
        updated = self.store.update_claim(claim.id, vault_id=1, claim_text="Updated", status="superseded")
        self.assertEqual(updated.claim_text, "Updated")
        self.assertEqual(updated.status, "superseded")


class TestWikiStoreEntityCRUD(unittest.TestCase):

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_upsert_entity_creates(self):
        entity = self.store.upsert_entity(
            vault_id=1,
            canonical_name="AFOMIS",
            entity_type="acronym",
            aliases=["Air Force Operational Medicine Information Systems"],
        )
        self.assertEqual(entity.canonical_name, "AFOMIS")
        self.assertIn("Air Force Operational Medicine Information Systems", entity.aliases)

    def test_upsert_entity_merges_aliases(self):
        self.store.upsert_entity(vault_id=1, canonical_name="AFOMIS", aliases=["Alias A"])
        entity = self.store.upsert_entity(vault_id=1, canonical_name="AFOMIS", aliases=["Alias B"])
        self.assertIn("Alias A", entity.aliases)
        self.assertIn("Alias B", entity.aliases)

    def test_entity_delete_cascades_relations(self):
        e1 = self.store.upsert_entity(vault_id=1, canonical_name="Org A")
        e2 = self.store.upsert_entity(vault_id=1, canonical_name="Person B")
        rel = self.store.create_relation(
            vault_id=1, predicate="chief", subject_entity_id=e1.id, object_entity_id=e2.id
        )
        # Delete entity e1 — relation should cascade
        self.conn.execute("DELETE FROM wiki_entities WHERE id = ?", (e1.id,))
        self.conn.commit()
        remaining = self.conn.execute(
            "SELECT * FROM wiki_relations WHERE id = ?", (rel.id,)
        ).fetchone()
        self.assertIsNone(remaining)


class TestWikiStoreFTSSearch(unittest.TestCase):

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_search_pages_by_title(self):
        self.store.create_page(vault_id=1, title="AFOMIS System Overview", page_type="overview")
        self.store.create_page(vault_id=1, title="Budget Report", page_type="manual")
        result = self.store.search(vault_id=1, query="AFOMIS")
        page_titles = [p.title for p in result["pages"]]
        self.assertIn("AFOMIS System Overview", page_titles)
        self.assertNotIn("Budget Report", page_titles)

    def test_search_claims_by_text(self):
        claim = self.store.create_claim(
            vault_id=1, claim_text="Justice Sakyi is AFOMIS Chief", source_type="manual"
        )
        result = self.store.search(vault_id=1, query="Justice Sakyi")
        claim_ids = [c.id for c in result["claims"]]
        self.assertIn(claim.id, claim_ids)

    def test_search_no_results(self):
        result = self.store.search(vault_id=1, query="xyznonexistent")
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["entities"], [])


def _seed_file(conn, vault_id=1, file_name="doc.pdf"):
    """Insert a minimal files row for the given vault; return its id."""
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size) VALUES (?, ?, ?, ?)",
        (vault_id, f"/tmp/{file_name}", file_name, 1024),
    )
    conn.commit()
    return cur.lastrowid


class TestWikiStoreVersionHistory(unittest.TestCase):
    """save_version + list_versions (DD-C003)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_update_page_snapshots_pre_update_state(self):
        page = self.store.create_page(
            vault_id=1, title="V1 Title", page_type="entity",
            markdown="body one", summary="sum one", status="draft",
        )
        self.assertEqual(self.store.list_versions(page.id), [])

        self.store.update_page(page.id, vault_id=1, title="V2 Title", markdown="body two")

        versions = self.store.list_versions(page.id)
        self.assertEqual(len(versions), 1)
        snap = versions[0]
        # Snapshot must capture the PRE-update state, not the new values.
        self.assertEqual(snap.title, "V1 Title")
        self.assertEqual(snap.markdown, "body one")
        self.assertEqual(snap.summary, "sum one")
        self.assertEqual(snap.status, "draft")
        self.assertEqual(snap.page_id, page.id)
        self.assertEqual(snap.vault_id, 1)

    def test_list_versions_newest_first(self):
        page = self.store.create_page(
            vault_id=1, title="Title A", page_type="entity", markdown="m-a"
        )
        # Each update snapshots the state just before that update.
        self.store.update_page(page.id, vault_id=1, title="Title B", markdown="m-b")
        self.store.update_page(page.id, vault_id=1, title="Title C", markdown="m-c")
        self.store.update_page(page.id, vault_id=1, title="Title D", markdown="m-d")

        versions = self.store.list_versions(page.id)
        self.assertEqual(len(versions), 3)
        # The three snapshots captured states A, B, C (D is current, never snapshotted).
        captured = {v.markdown for v in versions}
        self.assertEqual(captured, {"m-a", "m-b", "m-c"})
        # Newest-first: id is monotonically increasing, so versions should be
        # ordered by descending id (the most recently snapshotted = "m-c" first).
        ids = [v.id for v in versions]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(versions[0].markdown, "m-c")

    def test_save_version_nonexistent_page_returns_none(self):
        self.assertIsNone(self.store.save_version(99999, vault_id=1))

    def test_list_versions_respects_limit(self):
        page = self.store.create_page(vault_id=1, title="T", page_type="entity")
        for i in range(5):
            self.store.update_page(page.id, vault_id=1, markdown=f"v{i}")
        self.assertEqual(len(self.store.list_versions(page.id)), 5)
        self.assertEqual(len(self.store.list_versions(page.id, limit=2)), 2)


class TestWikiStoreFileAttachments(unittest.TestCase):
    """attach_file + list_page_files + detach_file (DD-C019)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_attach_list_detach(self):
        page = self.store.create_page(vault_id=1, title="P", page_type="entity")
        file_id = _seed_file(self.conn)

        attached = self.store.attach_file(page.id, file_id, vault_id=1)
        self.assertIsNotNone(attached)
        self.assertEqual(attached.page_id, page.id)
        self.assertEqual(attached.file_id, file_id)
        self.assertEqual(attached.vault_id, 1)

        files = self.store.list_page_files(page.id)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].file_id, file_id)

        self.assertTrue(self.store.detach_file(page.id, file_id, vault_id=1))
        self.assertEqual(self.store.list_page_files(page.id), [])

    def test_duplicate_attach_raises_integrity_error(self):
        # attach_file uses a plain INSERT so a duplicate (page_id, file_id) raises
        # sqlite3.IntegrityError (which the route maps to 409). The failed insert
        # is rolled back, leaving exactly one attachment row.
        page = self.store.create_page(vault_id=1, title="P", page_type="entity")
        file_id = _seed_file(self.conn)
        self.store.attach_file(page.id, file_id, vault_id=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.attach_file(page.id, file_id, vault_id=1)
        self.assertEqual(len(self.store.list_page_files(page.id)), 1)

    def test_duplicate_raw_insert_raises_integrity_error(self):
        # The UNIQUE(page_id, file_id) constraint is enforced at the schema level:
        # a raw duplicate INSERT (without OR IGNORE) must raise IntegrityError.
        page = self.store.create_page(vault_id=1, title="P", page_type="entity")
        file_id = _seed_file(self.conn)
        self.store.attach_file(page.id, file_id, vault_id=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO wiki_page_files (page_id, file_id, vault_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (page.id, file_id, 1, "2026-01-01T00:00:00"),
            )

    def test_detach_nonexistent_returns_false(self):
        page = self.store.create_page(vault_id=1, title="P", page_type="entity")
        self.assertFalse(self.store.detach_file(page.id, 99999, vault_id=1))


class TestWikiStoreLinksAndBacklinks(unittest.TestCase):
    """sync_page_links + list_backlinks (DD-C030, F-002, F-006)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_backlink_appears_after_sync(self):
        target = self.store.create_page(vault_id=1, title="Target Page", page_type="entity")
        source = self.store.create_page(vault_id=1, title="Source Page", page_type="entity")
        links = self.store.sync_page_links(
            source.id, vault_id=1, markdown=f"See [[{target.slug}]] for details."
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_page_id, target.id)

        backlinks = self.store.list_backlinks(target.id, vault_id=1)
        self.assertEqual(len(backlinks), 1)
        self.assertEqual(backlinks[0].source_page_id, source.id)

    def test_pipe_display_text_resolves_target_and_strips_display(self):
        # F-006: [[slug|Display Text]] resolves to the same target; the part after
        # the pipe is stored as the link_text, the part before is the target slug.
        target = self.store.create_page(vault_id=1, title="Target Page", page_type="entity")
        source = self.store.create_page(vault_id=1, title="Source Page", page_type="entity")
        links = self.store.sync_page_links(
            source.id, vault_id=1, markdown=f"Read [[{target.slug}|Human Label]] now.",
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_page_id, target.id)
        self.assertEqual(links[0].link_text, "Human Label")

        backlinks = self.store.list_backlinks(target.id, vault_id=1)
        self.assertEqual(len(backlinks), 1)
        self.assertEqual(backlinks[0].source_page_id, source.id)

    def test_backlinks_exclude_other_vaults(self):
        # F-002: list_backlinks is vault-scoped. The UNIQUE constraint is only on
        # (source_page_id, target_page_id), so a cross-vault link row with the same
        # target can exist — it must NOT be returned for vault 1.
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'Vault Two')")
        target = self.store.create_page(vault_id=1, title="Target Page", page_type="entity")
        source = self.store.create_page(vault_id=1, title="Source Page", page_type="entity")
        # A real same-vault backlink.
        self.store.sync_page_links(source.id, vault_id=1, markdown=f"[[{target.slug}]]")
        # A cross-vault row pointing at the same target but tagged vault_id=2.
        # Use a distinct source_page_id so it doesn't trip the UNIQUE constraint.
        other_src = self.store.create_page(vault_id=2, title="Other Vault Src", page_type="entity")
        self.conn.execute(
            "INSERT INTO wiki_page_links (source_page_id, target_page_id, vault_id, link_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (other_src.id, target.id, 2, "cross", "2026-01-01T00:00:00"),
        )
        self.conn.commit()

        backlinks_v1 = self.store.list_backlinks(target.id, vault_id=1)
        self.assertEqual(len(backlinks_v1), 1)
        self.assertEqual(backlinks_v1[0].source_page_id, source.id)
        self.assertEqual(backlinks_v1[0].vault_id, 1)

        backlinks_v2 = self.store.list_backlinks(target.id, vault_id=2)
        self.assertEqual(len(backlinks_v2), 1)
        self.assertEqual(backlinks_v2[0].vault_id, 2)

    def test_empty_and_whitespace_slugs_skipped(self):
        source = self.store.create_page(vault_id=1, title="Source Page", page_type="entity")
        # [[]] and [[   ]] normalize to empty slugs and must be skipped (not errors).
        links = self.store.sync_page_links(
            source.id, vault_id=1, markdown="noise [[]] and [[   ]] and [[unknown-slug]]",
        )
        # Empty slugs skipped; unknown-slug does not resolve to any page -> no links.
        self.assertEqual(links, [])

    def test_unresolved_slug_creates_no_link(self):
        source = self.store.create_page(vault_id=1, title="Source Page", page_type="entity")
        links = self.store.sync_page_links(
            source.id, vault_id=1, markdown="[[does-not-exist]]"
        )
        self.assertEqual(links, [])

    def test_resync_replaces_old_links(self):
        t1 = self.store.create_page(vault_id=1, title="Target One", page_type="entity")
        t2 = self.store.create_page(vault_id=1, title="Target Two", page_type="entity")
        source = self.store.create_page(vault_id=1, title="Source", page_type="entity")
        self.store.sync_page_links(source.id, vault_id=1, markdown=f"[[{t1.slug}]]")
        self.assertEqual(len(self.store.list_backlinks(t1.id, vault_id=1)), 1)
        # Re-sync with a different target should drop the old link.
        self.store.sync_page_links(source.id, vault_id=1, markdown=f"[[{t2.slug}]]")
        self.assertEqual(len(self.store.list_backlinks(t1.id, vault_id=1)), 0)
        self.assertEqual(len(self.store.list_backlinks(t2.id, vault_id=1)), 1)


class TestWikiStoreBulkOps(unittest.TestCase):
    """bulk_update_pages + bulk_delete_pages (DD-C025, F-007 atomicity)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_bulk_update_all_succeed(self):
        p1 = self.store.create_page(vault_id=1, title="A", page_type="entity")
        p2 = self.store.create_page(vault_id=1, title="B", page_type="entity")
        p3 = self.store.create_page(vault_id=1, title="C", page_type="entity")
        results = self.store.bulk_update_pages(
            [p1.id, p2.id, p3.id], vault_id=1, status="verified"
        )
        self.assertEqual(len(results), 3)
        for pid in (p1.id, p2.id, p3.id):
            self.assertEqual(self.store.get_page(pid).status, "verified")

    def test_bulk_delete_multiple(self):
        p1 = self.store.create_page(vault_id=1, title="A", page_type="entity")
        p2 = self.store.create_page(vault_id=1, title="B", page_type="entity")
        count = self.store.bulk_delete_pages([p1.id, p2.id], vault_id=1)
        self.assertEqual(count, 2)
        self.assertIsNone(self.store.get_page(p1.id))
        self.assertIsNone(self.store.get_page(p2.id))

    def test_bulk_update_rolls_back_on_mid_batch_failure(self):
        # F-007 atomicity: if any page in the batch raises, NOTHING is committed.
        # Force a mid-batch failure by monkeypatching update_page so the 2nd call
        # raises after the 1st has already executed its (uncommitted) UPDATE.
        p1 = self.store.create_page(vault_id=1, title="A", page_type="entity", status="draft")
        p2 = self.store.create_page(vault_id=1, title="B", page_type="entity", status="draft")

        real_update = self.store.update_page
        calls = {"n": 0}

        def flaky_update(pid, vault_id, commit=True, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated mid-batch failure")
            return real_update(pid, vault_id, commit=commit, **kwargs)

        self.store.update_page = flaky_update
        try:
            with self.assertRaises(RuntimeError):
                self.store.bulk_update_pages([p1.id, p2.id], vault_id=1, status="verified")
        finally:
            self.store.update_page = real_update

        # Rollback must have discarded p1's uncommitted UPDATE.
        self.assertEqual(self.store.get_page(p1.id).status, "draft")
        self.assertEqual(self.store.get_page(p2.id).status, "draft")

    def test_bulk_delete_rolls_back_on_mid_batch_failure(self):
        p1 = self.store.create_page(vault_id=1, title="A", page_type="entity")
        p2 = self.store.create_page(vault_id=1, title="B", page_type="entity")

        real_delete = self.store.delete_page
        calls = {"n": 0}

        def flaky_delete(pid, vault_id, commit=True):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated mid-batch failure")
            return real_delete(pid, vault_id, commit=commit)

        self.store.delete_page = flaky_delete
        try:
            with self.assertRaises(RuntimeError):
                self.store.bulk_delete_pages([p1.id, p2.id], vault_id=1)
        finally:
            self.store.delete_page = real_delete

        # Rollback must restore both pages (p1's uncommitted DELETE discarded).
        self.assertIsNotNone(self.store.get_page(p1.id))
        self.assertIsNotNone(self.store.get_page(p2.id))


class TestWikiStoreActivityLog(unittest.TestCase):
    """log_activity + list_activity (DD-C026)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_page_lifecycle_appends_activity(self):
        page = self.store.create_page(vault_id=1, title="P", page_type="entity")
        self.store.update_page(page.id, vault_id=1, title="P2")
        self.store.delete_page(page.id, vault_id=1)

        actions = [a.action for a in self.store.list_activity(vault_id=1)]
        self.assertIn("page_created", actions)
        self.assertIn("page_updated", actions)
        self.assertIn("page_deleted", actions)

    def test_list_activity_newest_first_and_limit(self):
        e1 = self.store.log_activity(1, "page_created", "page", 100)
        e2 = self.store.log_activity(1, "page_updated", "page", 100)
        e3 = self.store.log_activity(1, "page_deleted", "page", 100)
        entries = self.store.list_activity(vault_id=1)
        # Newest-first: monotonic ids => descending id order.
        ids = [e.id for e in entries]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(entries[0].id, e3.id)
        self.assertEqual(entries[-1].id, e1.id)
        # Limit bounds the result set.
        limited = self.store.list_activity(vault_id=1, limit=2)
        self.assertEqual(len(limited), 2)
        self.assertEqual(limited[0].id, e3.id)
        self.assertEqual(limited[1].id, e2.id)

    def test_activity_is_vault_scoped(self):
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'V2')")
        self.conn.commit()
        self.store.log_activity(1, "page_created", "page", 1)
        self.store.log_activity(2, "page_created", "page", 2)
        self.assertEqual(len(self.store.list_activity(vault_id=1)), 1)
        self.assertEqual(len(self.store.list_activity(vault_id=2)), 1)


class TestWikiStoreOptimisticLocking(unittest.TestCase):
    """update_page expected_version + TOCTOU conflict (F-005, DD-C020)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_correct_expected_version_bumps_version(self):
        page = self.store.create_page(vault_id=1, title="T", page_type="entity")
        self.assertEqual(page.version, 1)
        updated = self.store.update_page(
            page.id, vault_id=1, expected_version=1, title="T2"
        )
        self.assertEqual(updated.version, 2)
        # A second correct update bumps again.
        updated2 = self.store.update_page(
            page.id, vault_id=1, expected_version=2, title="T3"
        )
        self.assertEqual(updated2.version, 3)

    def test_stale_expected_version_raises_value_error(self):
        page = self.store.create_page(vault_id=1, title="T", page_type="entity")
        self.store.update_page(page.id, vault_id=1, title="T2")  # version -> 2
        with self.assertRaises(ValueError) as ctx:
            self.store.update_page(page.id, vault_id=1, expected_version=1, title="T3")
        self.assertIn("Version conflict", str(ctx.exception))
        # The failed update must not have changed the row.
        self.assertEqual(self.store.get_page(page.id).title, "T2")
        self.assertEqual(self.store.get_page(page.id).version, 2)

    def test_lost_update_toctou_conflict_path(self):
        # Simulate the lost-update window: a concurrent writer bumps the row's
        # version AFTER update_page reads it but BEFORE its UPDATE runs. The
        # rowcount==0 guard must turn this into an explicit conflict.
        page = self.store.create_page(vault_id=1, title="T", page_type="entity")

        real_save_version = self.store.save_version

        def bump_then_save(page_id, vault_id, edited_by=None, commit=True):
            # save_version is called inside update_page AFTER the version read and
            # BEFORE the UPDATE. Mutate the row's version here to invalidate the
            # pinned WHERE clause.
            self.conn.execute(
                "UPDATE wiki_pages SET version = version + 1 WHERE id = ?", (page_id,)
            )
            return real_save_version(page_id, vault_id, edited_by=edited_by, commit=commit)

        self.store.save_version = bump_then_save
        try:
            with self.assertRaises(ValueError) as ctx:
                # expected_version=None so we bypass the early check and reach the
                # pinned UPDATE / rowcount guard.
                self.store.update_page(page.id, vault_id=1, title="T2")
        finally:
            self.store.save_version = real_save_version
        self.assertIn("Version conflict", str(ctx.exception))

    def test_nonexistent_page_returns_none(self):
        self.assertIsNone(self.store.update_page(99999, vault_id=1, title="x"))


class TestWikiStoreClaimDedup(unittest.TestCase):
    """normalized_text dedup (F-004, DD-C011)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_create_claim_stores_normalized_text(self):
        claim = self.store.create_claim(
            vault_id=1, claim_text="Hello, World!  Extra   Spaces.", source_type="manual"
        )
        row = self.conn.execute(
            "SELECT normalized_text FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertEqual(row["normalized_text"], "hello world extra spaces")

    def test_find_by_exact_and_normalized(self):
        original = "Justice Sakyi is the AFOMIS Chief"
        claim = self.store.create_claim(vault_id=1, claim_text=original, source_type="manual")

        # Exact match locates it.
        self.assertEqual(self.store.find_claim_by_text(1, original).id, claim.id)
        # Normalized match also locates it.
        self.assertEqual(
            self.store.find_claim_by_normalized_text(1, original).id, claim.id
        )

        # A variant differing only by case/punctuation/extra spaces:
        variant = "justice  sakyi is THE afomis, chief!!"
        # Exact lookup must NOT find it.
        self.assertIsNone(self.store.find_claim_by_text(1, variant))
        # Normalized lookup MUST find the same claim.
        self.assertEqual(
            self.store.find_claim_by_normalized_text(1, variant).id, claim.id
        )

    def test_find_normalized_empty_returns_none(self):
        self.assertIsNone(self.store.find_claim_by_normalized_text(1, "!!!  "))

    def test_update_claim_refreshes_normalized_text(self):
        claim = self.store.create_claim(
            vault_id=1, claim_text="Original Text", source_type="manual"
        )
        self.store.update_claim(claim.id, vault_id=1, claim_text="Brand New, Claim!")
        row = self.conn.execute(
            "SELECT normalized_text FROM wiki_claims WHERE id = ?", (claim.id,)
        ).fetchone()
        self.assertEqual(row["normalized_text"], "brand new claim")
        # Old normalized text no longer locates the claim.
        self.assertIsNone(self.store.find_claim_by_normalized_text(1, "Original Text"))
        self.assertEqual(
            self.store.find_claim_by_normalized_text(1, "brand new claim").id, claim.id
        )

    def test_normalized_lookup_is_vault_scoped(self):
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'V2')")
        self.conn.commit()
        self.store.create_claim(vault_id=1, claim_text="Shared Claim", source_type="manual")
        self.assertIsNone(self.store.find_claim_by_normalized_text(2, "Shared Claim"))


class TestWikiStorePagination(unittest.TestCase):
    """list_entities / list_claims / list_jobs windowing & filtering (F-012)."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_list_entities_limit_offset(self):
        names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
        for n in names:
            self.store.upsert_entity(vault_id=1, canonical_name=n)
        # Ordered by canonical_name ASC.
        first_two = self.store.list_entities(1, limit=2, offset=0)
        self.assertEqual([e.canonical_name for e in first_two], ["Alpha", "Bravo"])
        next_two = self.store.list_entities(1, limit=2, offset=2)
        self.assertEqual([e.canonical_name for e in next_two], ["Charlie", "Delta"])
        # No limit returns all.
        self.assertEqual(len(self.store.list_entities(1)), 5)

    def test_list_claims_limit_offset(self):
        for i in range(5):
            self.store.create_claim(vault_id=1, claim_text=f"claim {i}", source_type="manual")
        page1 = self.store.list_claims(1, limit=2, offset=0)
        page2 = self.store.list_claims(1, limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        # Distinct windows (ordered by created_at DESC; ids monotonic).
        self.assertNotEqual(
            {c.id for c in page1}, {c.id for c in page2}
        )
        self.assertEqual(len(self.store.list_claims(1)), 5)

    def test_list_jobs_filter_and_bound(self):
        # trigger_type is constrained to the wiki_compile_jobs CHECK enum
        # ('ingest','query','memory','manual','settings_reindex').
        j1 = self.store.create_job(vault_id=1, trigger_type="manual", trigger_id="A")
        j2 = self.store.create_job(vault_id=1, trigger_type="manual", trigger_id="B")
        j3 = self.store.create_job(vault_id=1, trigger_type="query", trigger_id="A")

        # Filter by trigger_type.
        manual = self.store.list_jobs(1, trigger_type="manual")
        self.assertEqual({j.id for j in manual}, {j1.id, j2.id})
        # Filter by trigger_id.
        by_id = self.store.list_jobs(1, trigger_id="A")
        self.assertEqual({j.id for j in by_id}, {j1.id, j3.id})
        # Combined filter.
        combo = self.store.list_jobs(1, trigger_type="query", trigger_id="A")
        self.assertEqual({j.id for j in combo}, {j3.id})
        # Limit bounds the result set.
        bounded = self.store.list_jobs(1, limit=1)
        self.assertEqual(len(bounded), 1)
        self.assertIn(bounded[0].id, {j1.id, j2.id, j3.id})
        # Limit caps a larger set.
        self.assertEqual(len(self.store.list_jobs(1, limit=2)), 2)
        self.assertEqual(len(self.store.list_jobs(1)), 3)


class TestWikiStoreSearchFacetsAndHierarchy(unittest.TestCase):
    """search facets (page_type/status/sort_by) + parent_id hierarchy."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_search_filters_by_page_type(self):
        self.store.create_page(vault_id=1, title="AFOMIS overview", page_type="overview")
        self.store.create_page(vault_id=1, title="AFOMIS system", page_type="system")
        result = self.store.search(vault_id=1, query="AFOMIS", page_type="system")
        titles = [p.title for p in result["pages"]]
        self.assertEqual(titles, ["AFOMIS system"])

    def test_search_filters_by_status(self):
        self.store.create_page(
            vault_id=1, title="AFOMIS draft", page_type="overview", status="draft"
        )
        self.store.create_page(
            vault_id=1, title="AFOMIS verified", page_type="overview", status="verified"
        )
        result = self.store.search(vault_id=1, query="AFOMIS", status="verified")
        titles = [p.title for p in result["pages"]]
        self.assertEqual(titles, ["AFOMIS verified"])

    def test_search_sort_by_title_ascending(self):
        self.store.create_page(vault_id=1, title="AFOMIS Zebra", page_type="overview")
        self.store.create_page(vault_id=1, title="AFOMIS Apple", page_type="overview")
        result = self.store.search(vault_id=1, query="AFOMIS", sort_by="title")
        titles = [p.title for p in result["pages"]]
        self.assertEqual(titles, ["AFOMIS Apple", "AFOMIS Zebra"])

    def test_create_page_parent_id_round_trips(self):
        parent = self.store.create_page(vault_id=1, title="Parent", page_type="overview")
        child = self.store.create_page(
            vault_id=1, title="Child", page_type="entity", parent_id=parent.id
        )
        self.assertEqual(child.parent_id, parent.id)
        # Round-trips through get_page.
        self.assertEqual(self.store.get_page(child.id).parent_id, parent.id)
        # Root page has no parent.
        self.assertIsNone(parent.parent_id)


class TestWikiClaimsUniqueConstraint(unittest.TestCase):
    """Regression: wiki_claims UNIQUE(vault_id, claim_text) — issue #112."""

    def setUp(self):
        self.store, self.conn = _make_store()

    def tearDown(self):
        self.conn.close()

    def test_duplicate_claim_raises_integrity_error(self):
        """Inserting a claim with the same (vault_id, claim_text) raises."""
        import sqlite3

        self.store.create_claim(
            vault_id=1, claim_text="The sky is blue", source_type="manual"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_claim(
                vault_id=1, claim_text="The sky is blue", source_type="manual"
            )

    def test_same_claim_text_different_vault_is_allowed(self):
        """Same claim_text in different vaults is not a conflict."""
        self.store.create_claim(
            vault_id=1, claim_text="Shared fact", source_type="manual"
        )
        # vault 2 may not exist in FK terms but the UNIQUE index is on
        # (vault_id, claim_text), so a different vault_id is fine.
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'Vault 2')"
        )
        self.conn.commit()
        claim2 = self.store.create_claim(
            vault_id=2, claim_text="Shared fact", source_type="manual"
        )
        self.assertIsNotNone(claim2)

    def test_unique_index_exists_after_migration(self):
        """The unique index is present after run_migrations."""
        from app.models.database import run_migrations

        db_path = str(Path(tempfile.mkdtemp()) / "test_unique.db")
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        self.assertIn("idx_wiki_claims_unique_vault_claim", indexes)

    def test_migration_deduplicates_existing_rows(self):
        """Migration removes duplicate rows and creates the unique index."""
        from app.models.database import (
            migrate_add_wiki_claims_unique_claim_text,
        )

        db_path = str(Path(tempfile.mkdtemp()) / "test_dedup.db")
        # Build a minimal legacy schema WITHOUT the UNIQUE constraint to
        # simulate a database created before this fix.
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE vaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            );
            CREATE TABLE wiki_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                page_type TEXT NOT NULL DEFAULT 'entity',
                slug TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE wiki_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                claim_text TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'active',
                confidence REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V');
        """)
        conn.commit()
        # Insert duplicate rows (no unique constraint in this legacy schema).
        conn.execute(
            "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
            "VALUES (1, 'dup claim', 'manual', 'active')"
        )
        conn.execute(
            "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
            "VALUES (1, 'dup claim', 'manual', 'active')"
        )
        conn.commit()
        count_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE claim_text = 'dup claim'"
        ).fetchone()[0]
        self.assertEqual(count_before, 2)
        conn.close()

        # Run the migration — should deduplicate and create the unique index.
        migrate_add_wiki_claims_unique_claim_text(db_path)
        conn = sqlite3.connect(db_path)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE claim_text = 'dup claim'"
        ).fetchone()[0]
        self.assertEqual(count_after, 1)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_wiki_claims_unique_vault_claim'"
        ).fetchone()
        self.assertIsNotNone(idx)
        conn.close()


class TestWikiLintFindingJsonCheckConstraints(unittest.TestCase):
    """Regression test for issue #107: CHECK(json_type(...)=array) on JSON columns."""

    def _make_db(self) -> tuple:
        from app.models.database import run_migrations

        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "test.db")
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
        conn.commit()
        return conn, db_path

    def _make_old_schema_db(self) -> tuple:
        """Return (conn, db_path) with old schema (no CHECK constraints)."""
        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE wiki_lint_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                finding_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                title TEXT NOT NULL,
                details TEXT DEFAULT '',
                related_page_ids_json TEXT DEFAULT '[]',
                related_claim_ids_json TEXT DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        return conn, db_path

    def test_rejects_non_array_related_page_ids_json(self):
        """INSERT with a non-array value for related_page_ids_json must fail."""
        conn, _ = self._make_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO wiki_lint_findings
                   (vault_id, finding_type, severity, title, related_page_ids_json)
                   VALUES (1, 'orphan', 'low', 'test', '"not_an_array"')"""
            )
        conn.close()

    def test_rejects_non_array_related_claim_ids_json(self):
        """INSERT with a non-array value for related_claim_ids_json must fail."""
        conn, _ = self._make_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO wiki_lint_findings
                   (vault_id, finding_type, severity, title, related_claim_ids_json)
                   VALUES (1, 'orphan', 'low', 'test', '42')"""
            )
        conn.close()

    def test_rejects_null_in_json_column(self):
        """INSERT with NULL for related_page_ids_json must fail (not an array)."""
        conn, _ = self._make_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO wiki_lint_findings
                   (vault_id, finding_type, severity, title, related_page_ids_json)
                   VALUES (1, 'orphan', 'low', 'test', NULL)"""
            )
        conn.close()

    def test_accepts_valid_empty_array(self):
        """INSERT with '[]' should succeed (the default)."""
        conn, _ = self._make_db()
        conn.execute(
            """INSERT INTO wiki_lint_findings
               (vault_id, finding_type, severity, title)
               VALUES (1, 'orphan', 'low', 'test')"""
        )
        row = conn.execute(
            "SELECT related_page_ids_json, related_claim_ids_json "
            "FROM wiki_lint_findings WHERE vault_id = 1"
        ).fetchone()
        self.assertEqual(row[0], "[]")
        self.assertEqual(row[1], "[]")
        conn.close()

    def test_accepts_valid_nonempty_array(self):
        """INSERT with a valid JSON array of IDs should succeed."""
        conn, _ = self._make_db()
        conn.execute(
            """INSERT INTO wiki_lint_findings
               (vault_id, finding_type, severity, title,
                related_page_ids_json, related_claim_ids_json)
               VALUES (1, 'orphan', 'low', 'test', '[1,2,3]', '[10]')"""
        )
        row = conn.execute(
            "SELECT related_page_ids_json FROM wiki_lint_findings WHERE vault_id = 1"
        ).fetchone()
        self.assertEqual(row[0], "[1,2,3]")
        conn.close()

    def test_migration_idempotent(self):
        """Running the migration on an already-migrated database is a no-op."""
        from app.models.database import migrate_add_wiki_lint_findings_json_check

        conn, db_path = self._make_db()
        conn.close()
        # Should not raise on second run
        migrate_add_wiki_lint_findings_json_check(db_path)
        migrate_add_wiki_lint_findings_json_check(db_path)

    def test_migration_backfills_check_on_old_schema(self):
        """Migration adds CHECK constraints to a table created without them."""
        from app.models.database import migrate_add_wiki_lint_findings_json_check

        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE wiki_lint_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                finding_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                title TEXT NOT NULL,
                details TEXT DEFAULT '',
                related_page_ids_json TEXT DEFAULT '[]',
                related_claim_ids_json TEXT DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            """INSERT INTO wiki_lint_findings
               (vault_id, finding_type, severity, title,
                related_page_ids_json, related_claim_ids_json)
               VALUES (1, 'orphan', 'low', 'old finding', '[1]', '[2]')"""
        )
        conn.commit()
        conn.close()
        migrate_add_wiki_lint_findings_json_check(db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT title, related_page_ids_json FROM wiki_lint_findings"
        ).fetchone()
        self.assertEqual(row[0], "old finding")
        self.assertEqual(row[1], "[1]")

        # CHECK constraint now rejects non-array values
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO wiki_lint_findings
                   (vault_id, finding_type, severity, title, related_page_ids_json)
                   VALUES (1, 'orphan', 'low', 'bad', 'null')"""
            )
        conn.close()

    def test_migration_normalises_null_to_empty_array(self):
        """Migration coerces NULL values in JSON columns to '[]'."""
        from app.models.database import migrate_add_wiki_lint_findings_json_check

        conn, db_path = self._make_old_schema_db()
        conn.execute(
            "INSERT INTO wiki_lint_findings "
            "(vault_id, finding_type, severity, title, "
            " related_page_ids_json, related_claim_ids_json) "
            "VALUES (1, 'orphan', 'low', 'null-row', NULL, NULL)"
        )
        conn.commit()
        conn.close()

        migrate_add_wiki_lint_findings_json_check(db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT related_page_ids_json, related_claim_ids_json "
            "FROM wiki_lint_findings WHERE title='null-row'"
        ).fetchone()
        self.assertEqual(row[0], "[]")
        self.assertEqual(row[1], "[]")
        conn.close()

    def test_migration_normalises_non_array_json_to_empty_array(self):
        """Migration coerces non-array JSON ('null', '{}', '42') to '[]'."""
        from app.models.database import migrate_add_wiki_lint_findings_json_check

        conn, db_path = self._make_old_schema_db()
        rows = [
            (1, "orphan", "low", "json-null",    "null",    "null"),
            (1, "orphan", "low", "json-object",  "{}",      "{}"),
            (1, "orphan", "low", "json-number",  "42",      "42"),
            (1, "orphan", "low", "json-string",  '"hello"', '"world"'),
        ]
        conn.executemany(
            "INSERT INTO wiki_lint_findings "
            "(vault_id, finding_type, severity, title, "
            " related_page_ids_json, related_claim_ids_json) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

        migrate_add_wiki_lint_findings_json_check(db_path)

        conn = sqlite3.connect(db_path)
        migrated = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT title, related_page_ids_json, related_claim_ids_json "
                "FROM wiki_lint_findings"
            ).fetchall()
        }
        conn.close()
        for title in ("json-null", "json-object", "json-number", "json-string"):
            self.assertEqual(migrated[title], ("[]", "[]"), msg=f"row '{title}'")

    def test_migration_preserves_valid_arrays_and_normalises_invalid(self):
        """Migration preserves valid arrays; drifted values become '[]'."""
        from app.models.database import migrate_add_wiki_lint_findings_json_check

        conn, db_path = self._make_old_schema_db()
        conn.execute(
            "INSERT INTO wiki_lint_findings "
            "(vault_id, finding_type, severity, title, "
            " related_page_ids_json, related_claim_ids_json) "
            "VALUES (1, 'orphan', 'low', 'valid', '[1,2,3]', '[10]')"
        )
        conn.execute(
            "INSERT INTO wiki_lint_findings "
            "(vault_id, finding_type, severity, title, "
            " related_page_ids_json, related_claim_ids_json) "
            "VALUES (1, 'stale', 'medium', 'drifted', 'not-json', '{}')"
        )
        conn.commit()
        conn.close()

        migrate_add_wiki_lint_findings_json_check(db_path)

        conn = sqlite3.connect(db_path)
        migrated = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT title, related_page_ids_json, related_claim_ids_json "
                "FROM wiki_lint_findings"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(migrated["valid"],   ("[1,2,3]", "[10]"))
        self.assertEqual(migrated["drifted"], ("[]",      "[]"))

    def test_migration_recovery_from_partial_commit(self):
        """Recovery prologue handles a stale _wiki_lint_findings_old table."""
        from app.models.database import migrate_add_wiki_lint_findings_json_check

        # Simulate a prior run that crashed after RENAME but before CREATE:
        # _wiki_lint_findings_old exists, wiki_lint_findings does not.
        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE _wiki_lint_findings_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                finding_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                title TEXT NOT NULL,
                details TEXT DEFAULT '',
                related_page_ids_json TEXT DEFAULT '[]',
                related_claim_ids_json TEXT DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO _wiki_lint_findings_old "
            "(vault_id, finding_type, severity, title) "
            "VALUES (1, 'orphan', 'low', 'rescued')"
        )
        conn.commit()
        conn.close()

        migrate_add_wiki_lint_findings_json_check(db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT title, related_page_ids_json FROM wiki_lint_findings"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "rescued")
        self.assertEqual(row[1], "[]")
        stale = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_wiki_lint_findings_old'"
        ).fetchone()
        self.assertIsNone(stale)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO wiki_lint_findings "
                "(vault_id, finding_type, severity, title, related_page_ids_json) "
                "VALUES (1, 'orphan', 'low', 'bad', '{}')"
            )
        conn.close()
