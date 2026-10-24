"""Issue #975 — documents routes LIKE wildcard escaping (AC1 + AC2).

Mirrors the unified-search chat-title fix from #718 AC6
(``backend/tests/test_c13_unified_like_escape.py``) for the two documents
routes that bind the user term into ``LOWER(file_name) LIKE ?`` without an
``ESCAPE`` clause:

* ``GET /api/documents?search=...`` — three clause sites: the vault-scoped
  FTS arm, the global FTS arm, and the filename-only fallback arm used when
  the term has no FTS tokens (e.g. a bare ``%``);
* ``GET /api/documents/search?q=...`` — the ranked SQL's filename arm and
  the count-only re-derive SQL (both bind the same ``like_query``).

AC1: a literal ``%`` or ``_`` in the term must not act as a SQL wildcard
(``search=%`` with files named "alpha.pdf"/"beta.pdf" returns nothing).
AC2: plain terms keep returning the same substring matches as before.

RED at base (master), GREEN after the fix.
"""

from test_documents_auth import TestDocumentAuthBase


class TestListDocumentsLikeEscape(TestDocumentAuthBase):
    """GET /api/documents?search=... must treat % and _ literally."""

    def _seed_file(
        self, file_id, file_name, vault_id=2, parsed_text="giraffe locomotion notes"
    ):
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

    def setUp(self):
        super().setUp()
        # Three files in vault 2: two without any % or _ in the name, one with
        # both (the only legal AC1 hit for literal-wildcard queries). The
        # base class also seeds "test_doc.txt" (id 1, vault 2).
        self._seed_file(901, "alpha.pdf")
        self._seed_file(902, "beta.pdf")
        self._seed_file(903, "margin_50%.pdf")

    def _names(self, body):
        return [doc["file_name"] for doc in body["documents"]]

    def test_ac1_bare_percent_matches_only_literal_percent_names(self):
        """AC1 (filename-only arm): ``search=%`` must not match every file.

        No FTS tokens survive a bare ``%``, so this rides the fallback arm
        (documents.py ``extra_where`` filename-only LIKE). At base the bound
        pattern ``%%%`` matches every file name.
        """
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents", params={"search": "%"}, headers=self._auth_headers(token)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(
            data["total"], 1, f"'%%' matched {self._names(data)} as a wildcard"
        )
        self.assertEqual(self._names(data), ["margin_50%.pdf"])

    def test_ac1_underscore_literal_in_unscoped_fts_arm(self):
        """AC1 (global FTS arm): ``search=alp_a`` must not wildcard-match
        "alpha.pdf" (a,l,p,<any>,a). FTS token ``alp_a*`` matches nothing, so
        at base only the unescaped LIKE arm returns the file."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents",
            params={"search": "alp_a"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(
            data["total"], 0, f"'alp_a' wildcard-matched {self._names(data)}"
        )

    def test_ac1_underscore_literal_in_vault_scoped_fts_arm(self):
        """AC1 (vault-scoped FTS arm): same query with an explicit vault_id
        rides the vault-scoped clause set instead."""
        token = self._member_token()  # member1 has write access to vault 2
        resp = self.client.get(
            "/api/documents",
            params={"search": "alp_a", "vault_id": 2},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(
            data["total"], 0, f"'alp_a' wildcard-matched {self._names(data)}"
        )

    def test_ac2_plain_term_substring_matching_preserved(self):
        """AC2: plain terms keep returning the same substring matches (here:
        full token and mid-string substring) across the fixed arms."""
        token = self._superadmin_token()
        for term, expected in (("alpha", ["alpha.pdf"]), ("lph", ["alpha.pdf"])):
            resp = self.client.get(
                "/api/documents",
                params={"search": term},
                headers=self._auth_headers(token),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(
                self._names(data), expected, f"search={term!r} broke substring match"
            )

    def test_ac2_literal_underscore_and_percent_names_still_findable(self):
        """AC2 for the escaped characters themselves: a term matching the
        literal name must still hit (the escapes must not over-escape)."""
        token = self._superadmin_token()
        for term in ("margin_50", "50%", "50%.pdf"):
            resp = self.client.get(
                "/api/documents",
                params={"search": term},
                headers=self._auth_headers(token),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(
                self._names(data),
                ["margin_50%.pdf"],
                f"search={term!r} no longer finds the literal-% file",
            )


class TestSearchDocumentsLikeEscape(TestDocumentAuthBase):
    """GET /api/documents/search?q=... must treat % and _ literally."""

    def _seed_file(self, file_id, file_name, parsed_text, vault_id=2):
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

    def setUp(self):
        super().setUp()
        self._seed_file(911, "alpha.pdf", "unrelated body text one")
        self._seed_file(912, "beta.pdf", "unrelated body text two")
        self._seed_file(913, "notes_100%.txt", "zebra crossing patterns")
        # "report.pdf": mid-token substring "port" is reachable only via the
        # LIKE filename arm (FTS is token-prefix match, so ``port*`` cannot
        # hit the token "report") — the discriminator for a trailing %.
        self._seed_file(914, "report.pdf", "unrelated body text three")

    def test_ac1_trailing_percent_not_a_wildcard(self):
        """AC1 (ranked SQL filename arm): ``q=port%`` must require a literal
        ``%`` after "port"; at base the trailing ``%`` collapses into the
        surrounding wildcards and matches "report.pdf" via the LIKE arm (FTS
        ``port*`` cannot prefix-match the token "report")."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "port%"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(
            data["total"], 0, f"'port%%' wildcard-matched {data['results']}"
        )

    def test_ac1_underscore_not_a_wildcard(self):
        """AC1 (ranked SQL filename arm): ``q=alp_a`` must not wildcard-match
        "alpha.pdf"."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "alp_a"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(
            data["total"], 0, f"'alp_a' wildcard-matched {data['results']}"
        )

    def test_ac1_count_only_rederive_path_escaped(self):
        """AC1 (count-only SQL): a page beyond the result set re-derives
        ``total`` from the count-only statement, which binds the same
        ``like_query`` — it must not wildcard-match either."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "port%", "limit": 1, "offset": 10},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["results"], [])
        self.assertEqual(
            data["total"], 0, "count-only re-derive still wildcard-matched"
        )

    def test_ac2_plain_body_and_filename_matches_preserved(self):
        """AC2: plain terms keep returning body/metadata matches unchanged."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "zebra"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["id"], 913)
        self.assertEqual(data["results"][0]["match_type"], "body")

    def test_percent_only_query_still_rejected(self):
        """Guard: a bare ``%`` tokenizes to no FTS tokens, so the ranked
        search rejects it before any LIKE arm runs — unchanged by the fix."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "%"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 400, resp.text)


if __name__ == "__main__":
    import unittest

    unittest.main()
