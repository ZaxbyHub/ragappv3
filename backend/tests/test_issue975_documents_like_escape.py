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

The six AC1 wildcard tests are RED at base (master); the AC2/regression
guards pin preserved behavior and pass at both base and the fix.

The ``test_control_*`` tests are mutation-verified positive controls: each
asserts that a literal ``_``/backslash term still FINDS the file via the
escaped LIKE arm, so dropping that arm's ``ESCAPE`` clause (or the helper's
backslash-doubling step) flips the test to failure instead of silently
keeping the suite green.
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

    def test_control_literal_underscore_found_in_global_list_arm(self):
        """Positive control (global FTS arm): ``search=gin_50`` without a
        vault_id must FIND "margin_50%.pdf" via the escaped LIKE.

        FTS ``gin_50*`` prefix-matches nothing (the name's FTS tokens are
        "margin_50"/"pdf" — prefix match is anchored at token start), so the
        LIKE arm is the only matcher: pattern ``%gin\\_50%`` with
        ``ESCAPE '\\'``. If that arm's ESCAPE clause is dropped the pattern
        degenerates to a literal backslash (``gin\\_50`` substring — no file
        has one) and this test fails with total 0."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents",
            params={"search": "gin_50"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total"], 1, self._names(data))
        self.assertEqual(self._names(data), ["margin_50%.pdf"])

    def test_search_param_capped_at_200_chars(self):
        """The list route's ``search`` param is capped at 200 chars
        (mirroring ``/documents/search``'s ``q``): escaping roughly doubles
        special-dense terms, and past ~50k pattern chars SQLite raises
        "LIKE or GLOB pattern too complex" → uncaught 500. 201 chars is a
        422; the 200-char boundary still validates."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents",
            params={"search": "a" * 201},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        resp = self.client.get(
            "/api/documents",
            params={"search": "a" * 200},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)


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

    def test_control_literal_underscore_found_via_ranked_filename_arm(self):
        """Positive control (ranked SQL filename arm): ``q=otes_10`` must
        FIND "notes_100%.txt" — and only via the escaped LIKE.

        Mirrors test_ac1_trailing_percent_not_a_wildcard's arm isolation:
        FTS ``otes_10*`` prefix-matches nothing in either index (name tokens
        are "notes_100"/"txt"; the seeded bodies are zebra/unrelated text),
        so the filename LIKE arm is the only matcher: pattern ``%otes\\_10%``
        with ``ESCAPE '\\'`` → match_type "filename". If the ranked SQL's
        ESCAPE clause is dropped the pattern degenerates to a literal
        backslash substring and this test fails with total 0."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "otes_10"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total"], 1, data["results"])
        self.assertEqual(data["results"][0]["file_name"], "notes_100%.txt")
        self.assertEqual(data["results"][0]["match_type"], "filename")

    def test_control_literal_underscore_counted_in_count_only_rederive(self):
        """Positive control (count-only SQL): the same ``q=otes_10`` with a
        page beyond the result set (``limit=1&offset=10``) triggers the
        count-only re-derive statement, whose LIKE arm must still count the
        literal-underscore file. If that statement's ESCAPE clause is
        dropped the count degenerates to a literal-backslash substring,
        ``total`` falls to 0, and this test fails."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "otes_10", "limit": 1, "offset": 10},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["total"], 1, "count-only re-derive lost the hit")

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


class TestBackslashDoublingControl(TestDocumentAuthBase):
    """Positive control for the helper's backslash-doubling step.

    Seeds two confusable names: ``a\\b.pdf`` (literal backslash) and
    ``ab.pdf``. The probe ``q=a\\b`` tokenizes to the FTS query ``a* b*``,
    which matches ONLY ``a\\b.pdf`` ("ab.pdf" has no token starting with
    "b"), and the escaped LIKE pattern ``%a\\\\b%`` with ``ESCAPE '\\'``
    also matches only ``a\\b.pdf``. If the helper's ``.replace("\\\\",
    "\\\\\\\\")`` step is removed, the pattern becomes ``%a\\b% ESCAPE '\\'``
    — the escaped ``b`` reads as a literal "b", the LIKE arm matches
    ``ab.pdf``, and the extra file breaks this test.
    """

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
        self._seed_file(925, "a\\b.pdf", "unrelated body text four")
        self._seed_file(926, "ab.pdf", "unrelated body text five")

    def test_control_backslash_term_returns_only_literal_backslash_file(self):
        """``q=a\\b`` must return ONLY "a\\b.pdf" — never the confusable
        "ab.pdf" — across the ranked search's matcher union."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/documents/search",
            params={"q": "a\\b"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        names = [hit["file_name"] for hit in data["results"]]
        self.assertEqual(data["total"], 1, names)
        self.assertEqual(names, ["a\\b.pdf"], "confusable 'ab.pdf' leaked in")


class TestUnifiedSearchDocumentArmControl(TestDocumentAuthBase):
    """Positive control: the unified route's document arm inherits the
    escaped-LIKE behavior via ``search_documents`` (``GET /api/search/
    unified?types=document`` reuses the ranked documents handler verbatim),
    so a literal-underscore probe must find the literal-underscore file
    there too. Dropping the ranked SQL's ESCAPE clause flips this test.
    """

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
        self._seed_file(931, "alpha.pdf", "unrelated body text one")
        self._seed_file(932, "beta.pdf", "unrelated body text two")
        self._seed_file(933, "notes_100%.txt", "zebra crossing patterns")

    def test_control_unified_document_arm_finds_literal_underscore_file(self):
        """``GET /api/search/unified?types=document&q=otes_10`` must return
        exactly one document hit: "notes_100%.txt" (FTS ``otes_10*`` matches
        nothing, so the inherited escaped LIKE arm is the only matcher)."""
        token = self._superadmin_token()
        resp = self.client.get(
            "/api/search/unified",
            params={"q": "otes_10", "types": "document"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(len(data["results"]), 1, data["results"])
        self.assertEqual(data["results"][0]["type"], "document")
        self.assertEqual(data["results"][0]["title"], "notes_100%.txt")


if __name__ == "__main__":
    import unittest

    unittest.main()
