"""Route-level guard for the wiki lint resolve endpoint (issue #791).

`POST /wiki/lint/{finding_id}/resolve` must map the store's ValueError to
HTTP 400, so the system-only `auto_resolved` status is rejected at the route
and not only at the store layer. This lives in its own file because
`tests/test_wiki_routes.py` is byte-frozen by frozen check C9 (its blob is
pinned in the checkpoint manifest), so it cannot host new cases; the harness
is imported rather than duplicated, matching the repo's existing pattern.
"""

import unittest

from test_wiki_routes import WikiRouteTestBase


class TestLintResolveRouteRejectsSystemStatus(WikiRouteTestBase):
    """The route rejects a system-only status before touching the row."""

    def test_auto_resolved_status_is_rejected_with_400(self):
        # Status validation runs before the UPDATE, so the (missing) finding
        # id is irrelevant: an invalid status must surface as 400.
        resp = self.client.post(
            "/api/wiki/lint/999999/resolve",
            params={"vault_id": 1},
            json={"status": "auto_resolved"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("Invalid lint finding status", resp.json()["detail"])

    def test_valid_status_passes_validation_and_reaches_the_row_lookup(self):
        # Same missing id with a user-settable status: the 404 path proves the
        # status passed validation (an invalid status would have been a 400).
        resp = self.client.post(
            "/api/wiki/lint/999999/resolve",
            params={"vault_id": 1},
            json={"status": "dismissed"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_resolve_route_rejects_auto_resolved_even_for_an_existing_row(self):
        # Seed a real finding so the 400 cannot be confused with a
        # not-found short-circuit: the store must refuse the status.
        self.client.post(
            "/api/wiki/claims",
            json={
                "vault_id": 1,
                "claim_text": "Route guard claim",
                "source_type": "manual",
            },
        )
        run = self.client.post("/api/wiki/lint/run", json={"vault_id": 1})
        findings = run.json()["findings"]
        self.assertTrue(findings, "lint run should have produced a finding")
        finding_id = findings[0]["id"]
        resp = self.client.post(
            f"/api/wiki/lint/{finding_id}/resolve",
            params={"vault_id": 1},
            json={"status": "auto_resolved"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        # The row is untouched and still open.
        listed = self.client.get(
            "/api/wiki/lint", params={"vault_id": 1, "status": "open"}
        ).json()["findings"]
        self.assertIn(finding_id, [f["id"] for f in listed])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
