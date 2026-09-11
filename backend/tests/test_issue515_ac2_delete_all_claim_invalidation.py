"""Issue #515 acceptance tests — AC2 (WIKI-002).

Delete-all must persist wiki claim invalidation atomically.

Today ``delete_all_vault_documents`` purges file-derived data per file via
``_purge_file_derived_data`` → ``WikiStore.mark_claims_stale_by_file``
(which deliberately does NOT commit), then ``_atomic_delete`` rolls back any
open transaction before deleting the files rows — so the superseded-status
writes on claims whose only source was a deleted file are discarded.

Post-fix contract: after calling the delete-all route, the file row is gone
AND the sole-source claim row has status 'superseded', visible on a FRESH
connection (mirroring the single-file delete behavior where
``_delete_file_record`` commits the stale marking together with the row
delete).

Route-level harness mirrors ``test_documents_delete_audit.py`` (imported
base class, same pattern ``test_issue514_backend.py`` uses).
"""

import os
import sqlite3
import sys
import unittest

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

from test_documents_delete_audit import DocumentsDeleteAuditTestBase


class TestIssue515Ac2DeleteAllClaimInvalidation(DocumentsDeleteAuditTestBase):
    """AC2 (WIKI-002): delete-all must durably supersede sole-source claims."""

    def _seed_sole_source_claim(self, file_id: int, vault_id: int = 2):
        """Create a wiki page + claim whose ONLY source is the given file,
        using WikiStore directly (page/claim/source rows)."""
        from app.services.wiki_store import WikiStore

        conn = self._connection_pool.get_connection()
        try:
            store = WikiStore(conn)
            page = store.create_page(
                vault_id=vault_id,
                title=f"Topic for file {file_id}",
                page_type="entity",
                markdown="Derived topic page.",
            )
            claim = store.create_claim(
                vault_id=vault_id,
                claim_text=f"Fact sourced only from file {file_id}",
                source_type="document",
                page_id=page.id,
                status="active",
                sources=[
                    {
                        "source_kind": "document",
                        "file_id": file_id,
                        "source_label": f"file:{file_id}",
                        "quote": "fact",
                    }
                ],
            )
            return page.id, claim.id
        finally:
            self._connection_pool.release_connection(conn)

    def test_issue515_ac2_delete_all_persists_claim_invalidation(self):
        file_id = self._seed_file(
            vault_id=2, file_name="wiki-sourced.txt", parsed_text="source text"
        )
        page_id, claim_id = self._seed_sole_source_claim(file_id, vault_id=2)

        response = self.client.delete(
            "/api/documents/vault/2/all",
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text[:300])
        body = response.json()
        self.assertGreaterEqual(body.get("deleted_count", 0), 1)

        # FRESH connection: the invalidation must be durable, not discarded.
        fresh = sqlite3.connect(self._db_path)
        try:
            file_count = fresh.execute(
                "SELECT COUNT(*) FROM files WHERE id = ?", (file_id,)
            ).fetchone()[0]
            claim_status = fresh.execute(
                "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
            ).fetchone()[0]
        finally:
            fresh.close()

        self.assertEqual(
            file_count, 0, "delete-all must remove the files row"
        )
        self.assertEqual(
            claim_status,
            "superseded",
            "delete-all must durably mark sole-source wiki claims as "
            "superseded (atomic with the file deletion); a fresh connection "
            f"still sees status={claim_status!r}",
        )


if __name__ == "__main__":
    unittest.main()
