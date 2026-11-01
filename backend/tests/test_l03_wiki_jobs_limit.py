"""Issue #774 L03 red checkpoint (AC7): GET /api/wiki/jobs must bound its
result list with the ``limit`` query param.

The route currently declares no ``limit`` Query param (FastAPI silently drops
the unknown param) and calls ``store.list_jobs(vault_id, status=status)``
without a limit, so a client asking for the 10 most recent jobs gets every
job ever run. ``WikiStore.list_jobs`` already accepts ``limit`` — only the
route (and the frontend caller, AC6) need wiring.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_wiki_routes import WikiRouteTestBase


class TestWikiJobsLimit(WikiRouteTestBase):
    """GET /api/wiki/jobs?limit=N must return at most N jobs."""

    def setUp(self):
        super().setUp()
        # The base harness applies SCHEMA only; the production migrations that
        # add wiki_compile_jobs.input_json / retry_count are not run (same
        # fixture parity patch WikiNewRouteTestBase applies, so
        # WikiStore.create_job works on this DB regardless of the lease path).
        conn = self._raw()
        job_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(wiki_compile_jobs)").fetchall()
        }
        if "input_json" not in job_cols:
            conn.execute(
                "ALTER TABLE wiki_compile_jobs ADD COLUMN input_json TEXT DEFAULT '{}'"
            )
        if "retry_count" not in job_cols:
            conn.execute(
                "ALTER TABLE wiki_compile_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
        self._pool.release(conn)

    def test_limit_bounds_the_job_list(self):
        from app.services.wiki_store import WikiStore

        conn = self._raw()
        store = WikiStore(conn)
        for _ in range(3):
            store.create_job(vault_id=1, trigger_type="manual")
        self._pool.release(conn)

        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1, "limit": 2})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["jobs"]) == 2


if __name__ == "__main__":
    unittest.main()
