"""Tests for ranked ``GET /documents/search`` (Issue #396).

Verifies bm25 ranking, excerpt highlighting, deduplication before pagination,
and cross-vault isolation against a real SQLite FTS5 index.
"""

from test_documents_auth import TestDocumentAuthBase


class TestDocumentsSearchRanked(TestDocumentAuthBase):
    """GET /documents/search returns ranked docs with excerpts."""

    def _seed_doc(self, file_id, vault_id, file_name, parsed_text, status="indexed"):
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
                    status,
                    1,
                    vault_id,
                    parsed_text,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def test_body_match_returns_ranked_result_with_excerpt(self):
        self._seed_doc(
            200, 2, "fox_report.txt", "The quick brown fox jumps over the lazy dog."
        )
        token = self._member_token()
        resp = self.client.get(
            "/api/documents/search?q=fox&vault_id=2",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total"], 1)
        result = data["results"][0]
        self.assertEqual(result["id"], 200)
        self.assertEqual(result["match_type"], "body")
        self.assertIn("fox", result["excerpt"].lower())

    def test_filename_match_returns_metadata_match_type(self):
        # Body does not contain the search token; filename does. files_search_fts
        # indexes file_name, so a filename hit surfaces as match_type=metadata
        # (the LIKE arm is a fallback only for tokens FTS won't tokenize).
        self._seed_doc(
            201, 2, "unique_budget_file.txt", "Completely unrelated body text here."
        )
        token = self._member_token()
        resp = self.client.get(
            "/api/documents/search?q=budget&vault_id=2",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total"], 1)
        result = data["results"][0]
        self.assertEqual(result["match_type"], "metadata")
        self.assertEqual(result["id"], 201)

    def test_doc_matching_body_and_metadata_appears_once(self):
        """Dedup: a doc matching both body and metadata FTS appears once, and
        ``total`` counts unique docs, not union rows."""
        # Filename + body both contain the token.
        self._seed_doc(
            202, 2, "alpha_token.txt", "The alpha_token appears in the body too."
        )
        token = self._member_token()
        resp = self.client.get(
            "/api/documents/search?q=alpha_token&vault_id=2",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        ids = [r["id"] for r in data["results"]]
        self.assertEqual(ids.count(202), 1, "doc must appear exactly once after dedup")
        self.assertEqual(data["total"], 1, "total must count unique docs")

    def test_bm25_ranking_order_differs_from_insertion(self):
        """A doc with more/frequent matches ranks above one with a single mention."""
        # Strong match: token repeated.
        self._seed_doc(
            203, 2, "strong.txt", "flux flux flux flux flux the primary topic"
        )
        # Weak match: token once, in a longer document.
        self._seed_doc(
            204,
            2,
            "weak.txt",
            "flux " + "filler " * 200 + " only mentioned once at the end flux",
        )
        token = self._member_token()
        resp = self.client.get(
            "/api/documents/search?q=flux&vault_id=2",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(len(data["results"]), 2)
        # The strong-match doc (id 203) should rank first.
        self.assertEqual(data["results"][0]["id"], 203)

    def test_cross_vault_isolation_member_without_vault_id(self):
        """Member without an explicit vault_id must not see other-vault docs."""
        # Vault 3 doc — member1 has NO access to vault 3.
        self._seed_doc(
            205, 3, "vault3_secret.txt", "isolation_probe_token_xyz body"
        )
        token = self._member_token()  # member1 has access to vault 2 only
        resp = self.client.get(
            "/api/documents/search?q=isolation_probe_token_xyz",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        ids = [r["id"] for r in data["results"]]
        self.assertNotIn(
            205, ids, "cross-vault leak: vault 3 doc visible to vault 2 member"
        )

    def test_pagination_honored_post_dedup(self):
        """limit + offset operate on de-duplicated unique docs."""
        for i in range(5):
            self._seed_doc(
                210 + i,
                2,
                f"paginated_{i}.txt",
                f"pagination_probe token number {i}",
            )
        token = self._member_token()
        # Page 1
        resp1 = self.client.get(
            "/api/documents/search?q=pagination_probe&vault_id=2&limit=2&offset=0",
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp1.status_code, 200, resp1.text)
        page1 = resp1.json()
        self.assertEqual(len(page1["results"]), 2)
        self.assertEqual(page1["total"], 5)
        # Page 2 must not overlap page 1.
        resp2 = self.client.get(
            "/api/documents/search?q=pagination_probe&vault_id=2&limit=2&offset=2",
            headers=self._auth_headers(token),
        )
        page2 = resp2.json()
        ids1 = {r["id"] for r in page1["results"]}
        ids2 = {r["id"] for r in page2["results"]}
        self.assertEqual(len(page2["results"]), 2)
        self.assertEqual(ids1.isdisjoint(ids2), True, "pages must not overlap")

    def test_empty_query_rejected(self):
        token = self._member_token()
        resp = self.client.get(
            "/api/documents/search?q=&vault_id=2",
            headers=self._auth_headers(token),
        )
        # Pydantic min_length=1 → 422, or endpoint 400.
        self.assertIn(resp.status_code, (400, 422))

    def test_unauthenticated_returns_401(self):
        resp = self.client.get("/api/documents/search?q=test")
        self.assertEqual(resp.status_code, 401)
