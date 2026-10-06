"""Feedback-round pins (F-010 / PRR-018) for GET /api/wiki/jobs limit handling.

The frozen l03 spec pins only the bounded case (limit=2 of 3 jobs); this
non-frozen companion pins the remaining wire contract:
  - omitted limit -> full unbounded list (no hard-coded default bound);
  - limit=0 and limit=501 -> FastAPI 422 validation envelope (ge=1, le=500).

Subclasses WikiRouteTestBase exactly like test_l03_wiki_jobs_limit.py (the
direct-call harness in test_559_s2_c4_readers_unified.py is the reason that
suite passes limit=None explicitly — see #857's feedback round).
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

    def test_omitted_limit_returns_the_full_unbounded_list(self):
        self._seed_jobs(3)
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["jobs"]), 3)

    def test_limit_zero_is_rejected_with_422(self):
        self._seed_jobs(1)
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1, "limit": 0})
        self.assertEqual(resp.status_code, 422)

    def test_limit_above_500_is_rejected_with_422(self):
        self._seed_jobs(1)
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1, "limit": 501})
        self.assertEqual(resp.status_code, 422)
