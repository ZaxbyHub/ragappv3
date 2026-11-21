"""Feedback-round pins (F-010 / PRR-018) for GET /api/wiki/jobs limit handling.

The frozen l03 spec pins only the bounded case (limit=2 of 3 jobs); this
non-frozen companion pins the remaining wire contract:
  - omitted limit -> the DEFAULT PAGE (50 rows) — the bounded default the
    #1157 AC3 contract change introduced (this pin previously asserted the
    full unbounded list; the audit deliberately flipped that default, and
    this pin was rewritten with the contract, seeds >50 to stay
    non-vacuous, and still pins that an explicit larger limit fetches
    deeper);
  - limit=0 and limit=501 -> FastAPI 422 validation envelope (ge=1, le=500).

Subclasses WikiRouteTestBase exactly like test_l03_wiki_jobs_limit.py (the
direct-call harness in test_559_s2_c4_readers_unified.py is the reason that
suite passes limit=None explicitly — see #857's feedback round; that suite
predates the default and is unaffected because WikiStore keeps limit=None
meaning "no bound" at the store layer).
"""
from tests.test_wiki_routes import WikiRouteTestBase


class TestWikiJobsLimitFeedbackPins(WikiRouteTestBase):
    def _seed_jobs(self, count):
        from app.services.wiki_store import WikiStore

        conn = self._pool.get()
        try:
            store = WikiStore(conn)
            for _ in range(count):
                store.create_job(vault_id=1, trigger_type="manual")
            conn.commit()
        finally:
            self._pool.release(conn)

    def test_omitted_limit_returns_the_default_page(self):
        # >default seeds so the bound is non-vacuous: 60 jobs, omitted limit
        # returns exactly the 50-row default page (newest first).
        self._seed_jobs(60)
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["jobs"]), 50)

    def test_explicit_limit_fetches_beyond_the_default_page(self):
        self._seed_jobs(60)
        resp = self.client.get(
            "/api/wiki/jobs", params={"vault_id": 1, "limit": 60}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["jobs"]), 60)

    def test_limit_zero_is_rejected_with_422(self):
        self._seed_jobs(1)
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1, "limit": 0})
        self.assertEqual(resp.status_code, 422)

    def test_limit_above_500_is_rejected_with_422(self):
        self._seed_jobs(1)
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1, "limit": 501})
        self.assertEqual(resp.status_code, 422)
