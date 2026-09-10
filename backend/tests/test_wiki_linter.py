"""Tests for WikiLinter lint detection and findings lifecycle."""

import sqlite3
import tempfile
import unittest
from pathlib import Path


def _make_env():
    from app.models.database import run_migrations
    from app.services.wiki_linter import WikiLinter
    from app.services.wiki_store import WikiStore

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.commit()
    store = WikiStore(conn)
    linter = WikiLinter(conn, store)
    return conn, store, linter


class TestUnsupportedClaimDetection(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.linter = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_detects_claim_without_sources(self):
        self.store.create_claim(
            vault_id=1, claim_text="Unsupported claim text", source_type="manual"
        )
        findings = self.linter.run_lint(1)
        finding_types = {f.finding_type for f in findings}
        self.assertIn("unsupported_claim", finding_types)

    def test_no_unsupported_claim_when_source_attached(self):
        claim = self.store.create_claim(
            vault_id=1, claim_text="Supported claim", source_type="memory"
        )
        self.store.attach_source(
            claim_id=claim.id, source_kind="memory", memory_id=1
        )
        self.conn.commit()
        findings = self.linter.run_lint(1)
        unsupported = [f for f in findings if f.finding_type == "unsupported_claim"]
        self.assertEqual(len(unsupported), 0)

    def test_promoted_memory_claims_have_no_unsupported_finding(self):
        """End-to-end: promoted memory claims have sources so lint should not flag them."""
        from app.services.wiki_compiler import WikiCompiler

        compiler = WikiCompiler(self.conn, self.store)
        self.conn.execute(
            "INSERT INTO memories (content, vault_id) VALUES (?, ?)",
            (
                "AFOMIS stands for Air Force Operational Medicine Information Systems. "
                "Justice Sakyi is the AFOMIS Chief.",
                1,
            ),
        )
        self.conn.commit()
        mem_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        compiler.promote_memory(memory_id=mem_id, vault_id=1)

        findings = self.linter.run_lint(1)
        unsupported = [f for f in findings if f.finding_type == "unsupported_claim"]
        self.assertEqual(
            len(unsupported),
            0,
            f"Unexpected unsupported_claim findings: {[f.title for f in unsupported]}",
        )


class TestOrphanClaimDetection(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.linter = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_detects_orphan_claim(self):
        page = self.store.create_page(vault_id=1, title="P", page_type="entity")
        claim = self.store.create_claim(
            vault_id=1, claim_text="Orphan soon", source_type="manual", page_id=page.id
        )
        # Attach source so it doesn't also trigger unsupported_claim
        self.store.attach_source(claim_id=claim.id, source_kind="manual")
        self.conn.commit()
        # Delete page → claim.page_id becomes NULL
        self.store.delete_page(page.id, vault_id=1)

        findings = self.linter.run_lint(1)
        orphan_findings = [f for f in findings if f.finding_type == "orphan"]
        self.assertEqual(len(orphan_findings), 1)


class TestLintRunLifecycle(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.linter = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_run_lint_suppresses_previously_dismissed_findings(self):
        # Issue #515 AC35 (sanctioned rename/update of the old
        # test_run_lint_clears_prior_open_findings pin): run_lint now upserts
        # by fingerprint instead of clear-open-then-recreate, because the
        # issue mandates that a DISMISSED finding stop resurrecting as a
        # fresh open row on every subsequent run.
        self.store.create_claim(
            vault_id=1, claim_text="Unsupported 1", source_type="manual"
        )
        findings1 = self.linter.run_lint(1)
        self.assertGreater(len(findings1), 0)
        target = next(f for f in findings1 if f.finding_type == "unsupported_claim")

        dismissed = self.store.update_lint_finding(target.id, vault_id=1, status="dismissed")
        self.assertEqual(dismissed.status, "dismissed")

        # Second run still detects the same issue (claim unchanged) — the
        # dismissed row must NOT be re-created as open, and must remain the
        # only row for that fingerprint.
        findings2 = self.linter.run_lint(1)
        self.assertNotIn(target.id, [f.id for f in findings2])
        row = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE id = ?", (target.id,)
        ).fetchone()
        self.assertEqual(row["status"], "dismissed")
        dup_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings WHERE vault_id = 1 AND status = 'open'"
        ).fetchone()[0]
        self.assertEqual(
            dup_count,
            len(findings2),
            "open rows after the run must equal the returned findings "
            "(no suppressed duplicates)",
        )

        # Remove the underlying issue: open rows for issues that went away
        # must transition to 'resolved' (not linger, not be deleted).
        self.conn.execute("DELETE FROM wiki_claims WHERE vault_id = 1")
        self.conn.commit()
        findings3 = self.linter.run_lint(1)
        total_open = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings WHERE vault_id = 1 AND status = 'open'"
        ).fetchone()[0]
        self.assertEqual(total_open, len(findings3))
        self.assertEqual(total_open, 0, "no open findings should remain after the issue is gone")

    def test_duplicate_alias_detection(self):
        self.store.upsert_entity(vault_id=1, canonical_name="AFOMIS", aliases=["AirForce Med"])
        self.store.upsert_entity(vault_id=1, canonical_name="AFM", aliases=["AirForce Med"])
        self.conn.commit()
        findings = self.linter.run_lint(1)
        dup_findings = [f for f in findings if f.finding_type == "duplicate_entity"]
        self.assertGreater(len(dup_findings), 0)

    def test_conflicting_claims_detection(self):
        self.store.create_claim(
            vault_id=1, claim_text="A", source_type="manual",
            subject="AFOMIS", predicate="chief", object="Alice",
        )
        src1 = self.store.create_claim(
            vault_id=1, claim_text="B", source_type="manual",
            subject="AFOMIS", predicate="chief", object="Bob",
        )
        # Attach sources so unsupported_claim doesn't fire
        for claim in [src1]:
            self.store.attach_source(claim_id=claim.id, source_kind="manual")
        # First claim also needs source
        claim1 = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = 'A'"
        ).fetchone()[0]
        self.store.attach_source(claim_id=claim1, source_kind="manual")
        self.conn.commit()

        findings = self.linter.run_lint(1)
        contradiction_findings = [f for f in findings if f.finding_type == "contradiction"]
        self.assertGreater(len(contradiction_findings), 0)
