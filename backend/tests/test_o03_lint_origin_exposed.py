"""Lint-finding origin exposure on every read path (issue #949, #791 follow-up).

`wiki_lint_findings.origin` records the producing subsystem ('linter' /
'invalidation' / 'compiler' / 'curator') and the #791 reconciler scopes its
auto-resolve pass to ``origin='linter'`` rows — but until this issue the
column was write-only: ``WikiLintFinding`` did not carry it, so no read path
(list / run / resolve / per-page ``lint_findings``) could tell a reconciler
row from a curator row. These tests pin the field's presence and per-writer
value on every response surface, plus the unchanged pre-existing field set.

Lives in its own file (matching ``test_o01_lint_resolve_route.py``) because
``tests/test_wiki_routes.py`` is byte-frozen by frozen check C9; the harness
is imported rather than duplicated.
"""

import unittest

from test_wiki_routes import WikiRouteTestBase


class TestLintOriginExposed(WikiRouteTestBase):
    """Every lint-finding response carries the row's ``origin`` value."""

    def _seed_curator_finding(self, page_id=None) -> int:
        """Insert one OPEN curator-written row and return its id.

        Raw SQL on purpose: the AC is about the READ path, so the seed
        bypasses the store helpers and writes the row exactly as the curator
        subsystem would (INSERT ... origin='curator').
        """
        conn = self._raw()
        related_pages = f"[{page_id}]" if page_id is not None else "[]"
        cur = conn.execute(
            """INSERT INTO wiki_lint_findings
               (vault_id, finding_type, severity, title, details,
                related_page_ids_json, related_claim_ids_json, status, origin)
               VALUES (1, 'contradiction', 'high', 'Curator flagged a conflict',
                       '', ?, '[]', 'open', 'curator')""",
            (related_pages,),
        )
        conn.commit()
        return cur.lastrowid

    # AC1 -------------------------------------------------------------------

    def test_list_lint_findings_exposes_origin(self):
        finding_id = self._seed_curator_finding()
        resp = self.client.get("/api/wiki/lint", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        findings = resp.json()["findings"]
        match = [f for f in findings if f["id"] == finding_id]
        self.assertEqual(len(match), 1, "seeded curator finding must be listed")
        finding = match[0]
        self.assertIn("origin", finding)
        self.assertEqual(finding["origin"], "curator")

    # AC2 -------------------------------------------------------------------

    def test_origin_present_on_every_lint_surface(self):
        # A page so the per-page payload has a finding bound to it.
        page = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Origin Page", "page_type": "entity"},
        )
        self.assertEqual(page.status_code, 201, page.text)
        page_id = page.json()["id"]
        finding_id = self._seed_curator_finding(page_id=page_id)

        # A linter-detectable condition (manual claim with no sources) so the
        # run surface returns at least one linter-written row too.
        claim = self.client.post(
            "/api/wiki/claims",
            json={
                "vault_id": 1,
                "claim_text": "Claim with no sources",
                "source_type": "manual",
            },
        )
        self.assertEqual(claim.status_code, 201, claim.text)

        # Surface 1: GET /wiki/lint — curator row carries its origin.
        listed = self.client.get("/api/wiki/lint", params={"vault_id": 1}).json()["findings"]
        by_id = {f["id"]: f for f in listed}
        self.assertIn("origin", by_id[finding_id])
        self.assertEqual(by_id[finding_id]["origin"], "curator")

        # Surface 2: POST /wiki/lint/run — every returned finding (including
        # the linter's own) carries the field with the writer's value.
        run = self.client.post("/api/wiki/lint/run", json={"vault_id": 1})
        self.assertEqual(run.status_code, 200, run.text)
        run_findings = run.json()["findings"]
        self.assertTrue(run_findings, "lint run should have produced findings")
        for finding in run_findings:
            self.assertIn("origin", finding, finding)
        linter_rows = [f for f in run_findings if f["finding_type"] == "unsupported_claim"]
        self.assertTrue(linter_rows, "expected the linter's unsupported_claim row")
        for finding in linter_rows:
            self.assertEqual(finding["origin"], "linter")

        # Surface 3: POST /wiki/lint/{id}/resolve — the updated row keeps its
        # provenance ('acknowledged' is user-settable and does not auto-resolve).
        resolved = self.client.post(
            f"/api/wiki/lint/{finding_id}/resolve",
            params={"vault_id": 1},
            json={"status": "acknowledged"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertIn("origin", resolved.json())
        self.assertEqual(resolved.json()["origin"], "curator")

        # Surface 4: per-page lint_findings payload.
        detail = self.client.get(f"/api/wiki/pages/{page_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        page_findings = detail.json()["lint_findings"]
        page_match = [f for f in page_findings if f["id"] == finding_id]
        self.assertEqual(len(page_match), 1)
        self.assertIn("origin", page_match[0])
        self.assertEqual(page_match[0]["origin"], "curator")

    # AC4 (field-set preservation) -------------------------------------------

    def test_existing_lint_finding_fields_unchanged(self):
        """The additive field must not disturb the pre-#949 response shape."""
        finding_id = self._seed_curator_finding()
        finding = [
            f
            for f in self.client.get(
                "/api/wiki/lint", params={"vault_id": 1}
            ).json()["findings"]
            if f["id"] == finding_id
        ][0]
        self.assertEqual(
            set(finding.keys()),
            {
                "id",
                "vault_id",
                "finding_type",
                "severity",
                "title",
                "details",
                "related_page_ids_json",
                "related_claim_ids_json",
                "status",
                "created_at",
                "updated_at",
                "origin",
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
