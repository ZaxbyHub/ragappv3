"""Tests for document content-level (parsed_text body) search.

Phase 1.5: ``files_content_fts`` indexes ``files.parsed_text`` so the document
list search matches document *body* text, not just filename/metadata. These
tests verify the end-to-end route behaviour against a real SQLite FTS index.
"""

from test_documents_auth import TestDocumentAuthBase


class TestDocumentContentSearch(TestDocumentAuthBase):
    """GET /documents?search=... matches parsed_text body content."""

    def _seed_doc(self, file_id, vault_id, file_name, parsed_text):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, "
                "chunk_count, vault_id, parsed_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    file_name,
                    f"/uploads/{file_name}",
                    100,
                    "indexed",
                    1,
                    vault_id,
                    parsed_text,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def test_search_matches_body_text_not_in_metadata(self):
        # Body contains a unique token absent from the filename/metadata.
        self._seed_doc(
            100, 2, "quarterly_report.txt", "Revenue grew because of zlorptanium sales."
        )
        token = self._member_token()  # member1 has write/read on vault 2
        resp = self.client.get(
            "/api/documents?vault_id=2&search=zlorptanium",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200)
        names = [d["file_name"] for d in resp.json()["documents"]]
        self.assertIn("quarterly_report.txt", names)

    def test_search_non_matching_token_excludes_doc(self):
        self._seed_doc(
            101, 2, "quarterly_report.txt", "Revenue grew because of zlorptanium sales."
        )
        token = self._member_token()
        resp = self.client.get(
            "/api/documents?vault_id=2&search=nonexistentword",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200)
        names = [d["file_name"] for d in resp.json()["documents"]]
        self.assertNotIn("quarterly_report.txt", names)

    def test_filename_search_still_works(self):
        # Regression: metadata/filename search path is unaffected.
        self._seed_doc(102, 2, "budget_plan.txt", "Some unrelated body content here.")
        token = self._member_token()
        resp = self.client.get(
            "/api/documents?vault_id=2&search=budget",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200)
        names = [d["file_name"] for d in resp.json()["documents"]]
        self.assertIn("budget_plan.txt", names)
