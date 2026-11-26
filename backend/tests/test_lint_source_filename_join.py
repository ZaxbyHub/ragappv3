"""Backend tests for the lint source-filename join (issue #1161 D7).

The GET /wiki/lint enrichment joins each finding's source filename over
its related claims (claim -> sources -> files) and related pages
(page -> wiki_page_files -> files, with a page -> claims -> sources
fallback). These tests pin every relationship shape, the deterministic
multi-file choice, the NULL arm, and malformed-JSON tolerance.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db
from app.api.routes.wiki import _lint_source_filename
from app.main import app
from app.models.database import init_db, run_migrations
from app.security import csrf_protect
from app.services.wiki_store import WikiLintFinding


class TestLintSourceFilename(unittest.TestCase):
    def setUp(self):
        db_path = str(Path(tempfile.mkdtemp(prefix="lint-join-")) / "lj.db")
        init_db(db_path)
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        self.conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size, status, parsed_text) "
            "VALUES (1, 1, 'a.txt', 'alpha.txt', 1, 'indexed', 'body a')"
        )
        self.conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size, status, parsed_text) "
            "VALUES (2, 1, 'b.txt', 'beta.txt', 1, 'indexed', 'body b')"
        )
        # Claim 1 sourced from file 1.
        self.conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type, status) "
            "VALUES (1, 1, 'claim one', 'document', 'active')"
        )
        self.conn.execute(
            "INSERT INTO wiki_claim_sources (claim_id, source_kind, file_id, quote) "
            "VALUES (1, 'document', 1, 'q')"
        )
        # Page 10 attached to file 2; page 11 has a claim sourced from file 1.
        self.conn.execute(
            "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, markdown, status) "
            "VALUES (10, 1, 'p10', 'P10', 'entity', 'm', 'needs_review')"
        )
        self.conn.execute(
            "INSERT INTO wiki_page_files (page_id, file_id, vault_id) VALUES (10, 2, 1)"
        )
        self.conn.execute(
            "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, markdown, status) "
            "VALUES (11, 1, 'p11', 'P11', 'entity', 'm', 'needs_review')"
        )
        self.conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type, status, page_id) "
            "VALUES (2, 1, 'claim two', 'document', 'active', 11)"
        )
        self.conn.execute(
            "INSERT INTO wiki_claim_sources (claim_id, source_kind, file_id, quote) "
            "VALUES (2, 'document', 1, 'q2')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _finding(self, **overrides):
        base = dict(
            id=1,
            vault_id=1,
            finding_type="stale",
            severity="medium",
            title="t",
            details="",
            related_page_ids_json="[]",
            related_claim_ids_json="[]",
            status="open",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            origin="linter",
        )
        base.update(overrides)
        return WikiLintFinding(**base)

    def test_claim_related_finding_joins_through_sources(self):
        f = self._finding(related_claim_ids_json=json.dumps([1]))
        self.assertEqual(_lint_source_filename(self.conn, f), "alpha.txt")

    def test_page_related_finding_joins_through_page_files(self):
        f = self._finding(related_page_ids_json=json.dumps([10]))
        self.assertEqual(_lint_source_filename(self.conn, f), "beta.txt")

    def test_page_without_attachment_falls_back_to_claims_sources(self):
        f = self._finding(related_page_ids_json=json.dumps([11]))
        self.assertEqual(_lint_source_filename(self.conn, f), "alpha.txt")

    def test_unresolvable_finding_yields_none(self):
        f = self._finding(related_claim_ids_json=json.dumps([999]))
        self.assertIsNone(_lint_source_filename(self.conn, f))

    def test_malformed_json_columns_contribute_nothing(self):
        # The claim column is valid and resolvable; the page column is
        # malformed and must contribute nothing (no crash, no false joins).
        f = self._finding(
            related_claim_ids_json="[1]", related_page_ids_json="{not json"
        )
        self.assertEqual(_lint_source_filename(self.conn, f), "alpha.txt")

    def test_both_columns_malformed_yields_none(self):
        f = self._finding(
            related_claim_ids_json="[1,", related_page_ids_json="{not json"
        )
        self.assertIsNone(_lint_source_filename(self.conn, f))

    def test_route_response_carries_source_filename(self):
        self.conn.execute(
            "INSERT INTO wiki_lint_findings (id, vault_id, finding_type, severity, title, details, "
            "related_page_ids_json, related_claim_ids_json, status) "
            "VALUES (1, 1, 'stale', 'medium', 'Stale claim', '', '[]', '[1]', 'open')"
        )
        self.conn.commit()
        app.dependency_overrides[get_db] = lambda: self.conn
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1, "username": "admin", "role": "superadmin",
        }
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(app.dependency_overrides.pop, get_current_active_user, None)
        client = TestClient(app)
        resp = client.get("/api/wiki/lint?vault_id=1")
        self.assertEqual(resp.status_code, 200, resp.text)
        findings = resp.json()["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["source_filename"], "alpha.txt")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
