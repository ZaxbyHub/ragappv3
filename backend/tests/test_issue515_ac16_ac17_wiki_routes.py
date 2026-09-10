"""Issue #515 acceptance tests — AC16 (WIKI-011) and AC17 (API-002).

AC16: the per-document wiki status route derives linked pages ONLY from
claims, so a compiled plain document (page materialized, zero claims) is
reported as 'skipped' with pages_count=0. Post-fix the endpoint must report
status 'compiled' and include the created page (pages_count >= 1) — here
simulated exactly the way the compiler leaves the DB for a plain document:
a completed ingest job whose result_json names the page, the page row, and
the page-file association.

AC17: PUT /wiki/pages/{id} builds its update payload with
``model_dump(exclude_none=True)``, so an explicit ``{"parent_id": null}``
is dropped and can never clear the parent. Post-fix an explicit null must
clear the parent both in the response and in the DB, while an absent field
(``{}`` or title-only) must PRESERVE the parent.

Route-level harness mirrors ``test_wiki_routes.py``
(``WikiNewRouteTestBase``, same subclass style as its own suites).
"""

import json
import os
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

from test_wiki_routes import WikiNewRouteTestBase


class TestIssue515Ac16PlainDocumentWikiStatus(WikiNewRouteTestBase):
    """AC16 (WIKI-011): a compiled plain document must report 'compiled'."""

    def _simulate_compiled_plain_document(self, vault_id: int, file_id: int):
        """Leave the DB exactly like WikiCompiler.compile_ingest_job does for
        a plain document (page materialized, zero claims/entities): a
        completed ingest job whose result_json names the page, plus the page
        row and its page-file association."""
        from app.services.wiki_store import WikiStore, normalize_slug

        conn = self._raw()
        try:
            store = WikiStore(conn)
            file_name = f"plain-doc-{file_id}.txt"
            slug = normalize_slug(f"document/{file_name[:60]}")
            page = store.create_page(
                vault_id=vault_id,
                title=file_name,
                page_type="entity",
                slug=slug,
                markdown="Prose without extractable acronyms or role claims.",
                status="needs_review",
            )
            store.attach_file(page.id, file_id, vault_id)
            job = store.create_job(
                vault_id=vault_id,
                trigger_type="ingest",
                trigger_id=f"file:{file_id}",
                input_json={"file_id": file_id, "vault_id": vault_id},
            )
            conn.execute(
                "UPDATE wiki_compile_jobs SET status = 'completed', "
                "result_json = ? WHERE id = ?",
                (
                    json.dumps(
                        {
                            "page": {"id": page.id, "slug": slug},
                            "claims": [],
                            "entities": [],
                            "relations_count": 0,
                        }
                    ),
                    job.id,
                ),
            )
            conn.commit()
            return page
        finally:
            self._pool.release(conn)

    def test_issue515_ac16_plain_document_reports_compiled_with_page(self):
        file_id = self._insert_file(vault_id=1, file_name="plain-doc.txt")
        page = self._simulate_compiled_plain_document(vault_id=1, file_id=file_id)

        resp = self.client.get(
            f"/api/wiki/documents/{file_id}/status", params={"vault_id": 1}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()

        self.assertEqual(
            data["wiki_status"],
            "compiled",
            "a compiled plain document (page created, zero claims) must "
            f"report wiki_status='compiled', got {data['wiki_status']!r}",
        )
        self.assertGreaterEqual(
            data["pages_count"],
            1,
            "the document wiki-status must include the created page "
            f"(pages_count={data['pages_count']!r}, pages={data['pages']!r})",
        )
        self.assertIn(
            page.id,
            [p["id"] for p in data["pages"]],
            f"the compiled page must be listed; got {data['pages']!r}",
        )


class TestIssue515Ac17PageParentNullHandling(WikiNewRouteTestBase):
    """AC17 (API-002): explicit null clears the parent; absent keeps it."""

    def _db_parent_id(self, page_id):
        conn = self._raw()
        try:
            row = conn.execute(
                "SELECT parent_id FROM wiki_pages WHERE id = ?", (page_id,)
            ).fetchone()
            return row["parent_id"] if row else "MISSING"
        finally:
            self._pool.release(conn)

    def test_issue515_ac17_explicit_null_clears_parent_absent_preserves(self):
        parent = self._create_page(title="Parent Topic")
        child = self.client.post(
            "/api/wiki/pages",
            json={
                "vault_id": 1,
                "title": "Child Topic",
                "page_type": "entity",
                "parent_id": parent["id"],
            },
        )
        self.assertEqual(child.status_code, 201, child.text)
        child_id = child.json()["id"]
        self.assertEqual(child.json()["parent_id"], parent["id"])
        self.assertEqual(self._db_parent_id(child_id), parent["id"])

        # Explicit null must CLEAR the parent (response and DB).
        cleared = self.client.put(
            f"/api/wiki/pages/{child_id}", json={"parent_id": None}
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(
            cleared.json().get("parent_id"),
            f"explicit parent_id=null must clear the parent in the response; "
            f"got {cleared.json().get('parent_id')!r}",
        )
        self.assertIsNone(
            self._db_parent_id(child_id),
            "explicit parent_id=null must persist parent_id=NULL in the DB",
        )

        # Restore the parent, then an ABSENT parent_id (empty body / title
        # only) must PRESERVE it.
        restored = self.client.put(
            f"/api/wiki/pages/{child_id}", json={"parent_id": parent["id"]}
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["parent_id"], parent["id"])

        empty = self.client.put(f"/api/wiki/pages/{child_id}", json={})
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(
            empty.json().get("parent_id"),
            parent["id"],
            "an empty update body must PRESERVE the parent",
        )
        self.assertEqual(self._db_parent_id(child_id), parent["id"])

        retitled = self.client.put(
            f"/api/wiki/pages/{child_id}", json={"title": "Renamed Child"}
        )
        self.assertEqual(retitled.status_code, 200, retitled.text)
        self.assertEqual(
            retitled.json().get("parent_id"),
            parent["id"],
            "a title-only update must PRESERVE the parent",
        )
        self.assertEqual(self._db_parent_id(child_id), parent["id"])


if __name__ == "__main__":
    unittest.main()
