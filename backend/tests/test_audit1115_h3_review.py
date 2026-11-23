"""Unified reviewed-knowledge queue tests (issue #1163, Workstream L PR 7).

Frozen-check spec backing acceptance checks C10/C11/C12 (trace
1161-1162-1163-wiki-compiler-review-queue): the merged queue, the atomic
batch mutation, and the write-access gate. Additional in-scope behavior
(from the issue body): mixed wiki+KMS items with source quotes, status and
kind filters, mid-batch failure leaving everything unchanged, foreign /
missing / wrong-status rejections, and approve/reject through the route
with both stores inspected afterwards.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db
from app.main import app
from app.models.database import init_db, run_migrations
from app.security import csrf_protect


def _seed_role_claim_text(name: str = "Alice Smith", org: str = "AFOMIS", role: str = "Director") -> str:
    # Issue #1162: derived records only materialize for qualifying text.
    return f"{name} is the {org} {role}."


class _ReviewQueueTestBase(unittest.TestCase):
    """JWT-free harness: dependency overrides + a real temp DB."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3-review-queue-")
        db_path = str(Path(self.tmp) / "review-queue.db")
        init_db(db_path)
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

        self.conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (1, 'admin', 'x', 'Admin', 'superadmin', 1)"
        )
        self.conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (3, 'member1', 'x', 'Member One', 'member', 1)"
        )
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        self.conn.execute("INSERT INTO vaults (id, name) VALUES (3, 'Other')")
        # member1 has WRITE access to vault 1 only.
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) "
            "VALUES (1, 3, 'write', 1)"
        )
        # A viewer (read-only) exercises the 403 leg.
        self.conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (4, 'viewer1', 'x', 'Viewer', 'member', 1)"
        )
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) "
            "VALUES (1, 4, 'read', 1)"
        )

        # A qualifying document (file 1, vault 1) feeding both derived records.
        self.conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size, status, parsed_text) "
            "VALUES (1, 1, 'docs/one.txt', 'one.txt', 1, 'indexed', ?)",
            (_seed_role_claim_text(),),
        )
        self.conn.execute(
            "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, markdown, status) "
            "VALUES (1, 1, 'document/one', 'One', 'entity', 'body', 'needs_review')"
        )
        self.conn.execute(
            "INSERT INTO wiki_page_files (page_id, file_id, vault_id) VALUES (1, 1, 1)"
        )
        self.conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type, page_id, status, subject) "
            "VALUES (1, 1, 'Alice Smith is the AFOMIS Director.', 'document', 1, 'active', 'AFOMIS')"
        )
        self.conn.execute(
            "INSERT INTO wiki_claim_sources (claim_id, source_kind, file_id, quote) "
            "VALUES (1, 'document', 1, 'Alice Smith is the AFOMIS Director.')"
        )
        self.conn.execute(
            "INSERT INTO kms_entries (id, vault_id, file_id, slug, title, body, source_type, status) "
            "VALUES (1, 1, 1, 'doc-one', 'Doc One', 'body', 'document', 'draft')"
        )
        # A manual KMS entry (NOT a queue item) and an already-reviewed page.
        self.conn.execute(
            "INSERT INTO kms_entries (id, vault_id, slug, title, body, source_type, status) "
            "VALUES (2, 1, 'manual', 'Manual', 'body', 'manual', 'draft')"
        )
        self.conn.execute(
            "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, markdown, status) "
            "VALUES (2, 1, 'document/two', 'Two', 'entity', 'body', 'verified')"
        )
        # A foreign-vault page (existence must not leak; batch must reject).
        self.conn.execute(
            "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, markdown, status) "
            "VALUES (3, 3, 'document/foreign', 'Foreign', 'entity', 'body', 'needs_review')"
        )
        self.conn.commit()

        app.dependency_overrides[get_db] = lambda: self.conn
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        self._user = {"id": 1, "username": "admin", "role": "superadmin"}
        app.dependency_overrides[get_current_active_user] = lambda: self._user
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(app.dependency_overrides.pop, get_current_active_user, None)
        self.addCleanup(app.dependency_overrides.pop, csrf_protect, None)
        self.addCleanup(self.conn.close)

    def _counts(self):
        page = self.conn.execute(
            "SELECT status FROM wiki_pages WHERE id = 1"
        ).fetchone()["status"]
        entry = self.conn.execute(
            "SELECT status FROM kms_entries WHERE id = 1"
        ).fetchone()["status"]
        return page, entry


class TestReviewQueueMerged(_ReviewQueueTestBase):
    def test_review_queue_merges_wiki_and_kms(self):
        """AC11 / C10: one list carries BOTH awaiting-review kinds, each with
        its source quote; non-queue rows (manual entries, reviewed pages,
        foreign vaults) stay out."""
        resp = self.client.get("/api/review/queue?vault_id=1")
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        kinds = {(i["kind"], i["id"]) for i in items}
        self.assertIn(("wiki_page", 1), kinds)
        self.assertIn(("kms_entry", 1), kinds)
        self.assertEqual(len(items), 2, items)
        by_key = {(i["kind"], i["id"]): i for i in items}
        self.assertEqual(
            by_key[("wiki_page", 1)]["source_quote"],
            "Alice Smith is the AFOMIS Director.",
        )
        self.assertIn(
            "AFOMIS Director", by_key[("kms_entry", 1)]["source_quote"] or ""
        )

    def test_kind_and_status_filters(self):
        """Additional in-scope: filter across kind and status."""
        resp = self.client.get("/api/review/queue?vault_id=1&kind=kms_entry")
        self.assertEqual(resp.status_code, 200)
        kinds = {i["kind"] for i in resp.json()["items"]}
        self.assertEqual(kinds, {"kms_entry"})
        resp = self.client.get(
            "/api/review/queue?vault_id=1&status=needs_review"
        )
        self.assertEqual(resp.status_code, 200)
        statuses = {i["status"] for i in resp.json()["items"]}
        self.assertEqual(statuses, {"needs_review"})


class TestReviewQueueBatch(_ReviewQueueTestBase):
    def test_batch_approve_is_atomic(self):
        """AC12 / C11: one request updates every item or none."""
        before = self._counts()
        self.assertEqual(before, ("needs_review", "draft"))
        resp = self.client.post(
            "/api/review/queue/batch",
            json={
                "vault_id": 1,
                "action": "approve",
                "items": [
                    {"kind": "wiki_page", "id": 1},
                    {"kind": "kms_entry", "id": 1},
                ],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["updated"], 2)
        self.assertEqual(self._counts(), ("verified", "published"))

    def test_mid_batch_failure_leaves_everything_unchanged(self):
        """Additional in-scope: force failure mid-batch; ALL rows unchanged."""
        before = self._counts()
        resp = self.client.post(
            "/api/review/queue/batch",
            json={
                "vault_id": 1,
                "action": "approve",
                "items": [
                    {"kind": "wiki_page", "id": 1},
                    {"kind": "kms_entry", "id": 999},  # missing -> rollback
                ],
            },
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._counts(), before)
        row = self.conn.execute(
            "SELECT version, updated_at FROM wiki_pages WHERE id = 1"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_foreign_and_wrong_status_items_rejected(self):
        """Additional in-scope: foreign-vault, manual-entry, and
        already-reviewed targets are refused inside the transaction."""
        for items in (
            [{"kind": "wiki_page", "id": 3}],          # foreign vault
            [{"kind": "kms_entry", "id": 2}],          # manual entry
            [{"kind": "wiki_page", "id": 2}],          # already reviewed
            [{"kind": "kms_entry", "id": 1}, {"kind": "wiki_page", "id": 1},
             {"kind": "kms_entry", "id": 1}],          # duplicate item
        ):
            resp = self.client.post(
                "/api/review/queue/batch",
                json={"vault_id": 1, "action": "reject", "items": items},
            )
            self.assertIn(resp.status_code, (400, 404), (items, resp.text))
        self.assertEqual(self._counts(), ("needs_review", "draft"))

    def test_reject_via_route_archives_both_stores(self):
        """Additional in-scope: reject through the route, inspect both
        stores."""
        resp = self.client.post(
            "/api/review/queue/batch",
            json={
                "vault_id": 1,
                "action": "reject",
                "items": [
                    {"kind": "wiki_page", "id": 1},
                    {"kind": "kms_entry", "id": 1},
                ],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._counts(), ("archived", "archived"))


class TestReviewQueueAccess(_ReviewQueueTestBase):
    def test_review_queue_requires_write_access(self):
        """AC13 / C12: a read-only caller gets 403 on BOTH the queue and the
        batch action."""
        self._user.update({"id": 4, "username": "viewer1", "role": "member"})
        resp = self.client.get("/api/review/queue?vault_id=1")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            "/api/review/queue/batch",
            json={
                "vault_id": 1,
                "action": "approve",
                "items": [{"kind": "wiki_page", "id": 1}],
            },
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._counts(), ("needs_review", "draft"))

    def test_batch_version_snapshot_preserved(self):
        """Store-lifecycle parity: the batch update bumps version and
        snapshots the prior page (wiki_page_versions), like the single-item
        flow."""
        versions_before = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_page_versions WHERE page_id = 1"
        ).fetchone()[0]
        resp = self.client.post(
            "/api/review/queue/batch",
            json={
                "vault_id": 1,
                "action": "approve",
                "items": [{"kind": "wiki_page", "id": 1}],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        versions_after = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_page_versions WHERE page_id = 1"
        ).fetchone()[0]
        self.assertGreater(versions_after, versions_before)
        version = self.conn.execute(
            "SELECT version FROM wiki_pages WHERE id = 1"
        ).fetchone()["version"]
        self.assertGreaterEqual(version, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
