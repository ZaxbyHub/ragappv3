"""Bounded-admission checks for the wiki lint run route (issue #1117 AC1).

Harness mirrors backend/tests/test_o06c_wiki_lint_route_offloop.py: the
public route coroutine ``run_wiki_lint`` is driven directly (like a
regression probe) with a permissive evaluate, a real in-memory-backed
sqlite3 connection from a migrated temp DB, and ``WikiLinter`` stubbed so
``run_lint`` sleeps a fixed duration instead of scanning.

Frozen spec the implementer must satisfy (NEW-SURFACE, issue #1117 AC1):

- ``backend/app/api/routes/wiki.py`` exposes a module-level integer bound
  ``_WIKI_LINT_MAX_CONCURRENT_RUNS`` that the handler reads AT REQUEST
  TIME from the module namespace (so tests and operators can patch it
  per-run; a bound captured at import time -- e.g. baked into a semaphore
  constructed at module load -- does NOT satisfy this spec).
- Admission is checked BEFORE the request waits on any scan slot: while
  the number of in-flight runs (admitted, not yet responded) is at the
  bound, the handler raises ``fastapi.HTTPException`` with a 503-family
  status code (503 or 504) without queueing on the lint executor. The
  bound counts ADMISSIONS, not executor starts -- an admitted-but-queued
  run still pins its pooled DB connection, which is exactly the resource
  the bound must protect (OOB review F-02).

Checks in this file:

- C1 (AC1 of #1117, NEW-SURFACE): with the bound patched to B and B+K
  concurrent requests, exactly B succeed with findings, exactly K are
  rejected with a 503-family HTTPException, and every rejection completes
  in under ONE scan duration -- rejection happens at admission time, not
  queue-drain time. RED at base (the bound constant does not exist;
  every request is admitted unconditionally), GREEN after the fix.
- C1 preserving arm (NEW-SURFACE pin): with the bound patched to B and
  exactly B concurrent requests, all B succeed -- the bound must not
  reject within-bound bursts. GREEN at base (patch is created with
  create=True and the route ignores it), GREEN after the fix.
"""

import asyncio
import shutil
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

# One stub scan holds its executor slot for SCAN_SECONDS. The lint
# executor is a single worker, so no scan slot can free before
# SCAN_SECONDS elapses -- a rejection completing in under SCAN_SECONDS
# provably never waited for one (a queue-drain-timed rejection needs at
# least one full scan to finish first).
SCAN_SECONDS = 0.35
BOUND = 2
EXCESS = 3

_USER = {"id": 1, "username": "checker", "role": "superadmin", "is_active": True}


async def _allow_all(user, resource_type, resource_id, action):
    del user, resource_type, resource_id, action
    return True


def _connect(db_path):
    # check_same_thread=False: matches the production pool connections the
    # route handlers run against (the stub scan runs on the executor
    # thread while the handler holds this connection).
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestWikiLintRunBoundedAdmission(unittest.IsolatedAsyncioTestCase):
    """C1 (AC1 of issue #1117, NEW-SURFACE) -- RED at base, GREEN after
    the fix (discriminating arm), plus the within-bound preserving arm
    (GREEN at base and after the fix)."""

    async def asyncSetUp(self):
        from app.models.database import run_migrations

        self._tmp_root = Path(mkdtemp())
        self.db_path = str(self._tmp_root / "lint-admission.db")
        run_migrations(self.db_path)
        self.conn = _connect(self.db_path)
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')"
        )
        self.conn.commit()

    async def asyncTearDown(self):
        self.conn.close()
        shutil.rmtree(self._tmp_root, ignore_errors=True)

    def _make_linter(self, scan_calls):
        class _SlowLinter:
            def __init__(self, db, store):
                self._db = db
                self._store = store

            def run_lint(self, vault_id):
                scan_calls.append(vault_id)
                time.sleep(SCAN_SECONDS)
                return [{"finding_type": "orphan", "title": "admission probe"}]

        return _SlowLinter

    async def test_burst_beyond_bound_rejects_excess_at_admission(self):
        # check: C1 (AC1 of issue #1117, NEW-SURFACE) -- RED at base,
        # GREEN after the fix.
        from fastapi import HTTPException

        from app.api.routes import wiki as wiki_routes
        from app.api.routes.wiki import LintRunRequest, run_wiki_lint

        scan_calls: list = []
        outcomes: list = []

        async def _run_one():
            began = time.monotonic()
            try:
                response = await run_wiki_lint(
                    request=LintRunRequest(vault_id=1),
                    db=self.conn,
                    user=_USER,
                    evaluate=_allow_all,
                    _csrf_token="x",
                )
            except HTTPException as exc:
                outcomes.append(("rejected", time.monotonic() - began, exc))
            else:
                outcomes.append(("ok", time.monotonic() - began, response))

        # patching the module constant is only observable when the route
        # reads it at call time -- that observability IS the spec.
        with patch.object(
            wiki_routes, "_WIKI_LINT_MAX_CONCURRENT_RUNS", BOUND
        ), patch.object(wiki_routes, "WikiLinter", self._make_linter(scan_calls)):
            await asyncio.gather(
                *[_run_one() for _ in range(BOUND + EXCESS)]
            )

        rejected = [o for o in outcomes if o[0] == "rejected"]
        succeeded = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(
            len(succeeded), BOUND,
            f"exactly the bound ({BOUND}) concurrent runs may succeed; "
            f"got {len(succeeded)}",
        )
        self.assertEqual(
            len(rejected), EXCESS,
            f"exactly {EXCESS} requests must be rejected; got "
            f"{len(rejected)} (unbounded admission accepts all "
            f"{BOUND + EXCESS})",
        )
        for _kind, _elapsed, exc in rejected:
            self.assertIsInstance(exc, HTTPException)
            self.assertIn(
                exc.status_code, (503, 504),
                "saturation must reject with a 503-family status, got "
                f"{exc.status_code}",
            )
            # Admission-time rejection: no scan slot can free before the
            # first SCAN_SECONDS scan completes on the single-worker
            # executor, so a rejection observed in under SCAN_SECONDS
            # never waited for a slot. A queue-drain-timed rejection
            # cannot beat one full scan duration.
            self.assertLess(
                _elapsed, SCAN_SECONDS,
                "rejection must happen at admission time, not after "
                "waiting for a scan slot (queue-drain rejection)",
            )
        # Anti-tautology guard: the stub scan actually executed through
        # the route -- exactly BOUND times. The rejected requests never
        # reached a scan.
        self.assertEqual(
            scan_calls, [1] * BOUND,
            "exactly the admitted bound may execute a scan; rejected "
            "requests must never reach run_lint",
        )
        for _kind, _elapsed, response in succeeded:
            self.assertEqual(response["count"], 1)

    async def test_burst_within_bound_all_succeed(self):
        # check: C1 preserving arm (AC1 of issue #1117, NEW-SURFACE pin)
        # -- GREEN at base (the patch is created and the route ignores
        # it, so every request succeeds), GREEN after the fix (the bound
        # must not reject within-bound bursts).
        from app.api.routes import wiki as wiki_routes
        from app.api.routes.wiki import LintRunRequest, run_wiki_lint

        scan_calls: list = []

        async def _run_one():
            return await run_wiki_lint(
                request=LintRunRequest(vault_id=1),
                db=self.conn,
                user=_USER,
                evaluate=_allow_all,
                _csrf_token="x",
            )

        # create=True keeps this pin executable at base (the constant is
        # part of the not-yet-existing surface); after the fix the same
        # patch exercises the real bound.
        with patch.object(
            wiki_routes, "_WIKI_LINT_MAX_CONCURRENT_RUNS", BOUND, create=True
        ), patch.object(wiki_routes, "WikiLinter", self._make_linter(scan_calls)):
            responses = await asyncio.gather(
                *[_run_one() for _ in range(BOUND)]
            )

        self.assertEqual(len(responses), BOUND)
        for response in responses:
            self.assertEqual(response["count"], 1)
        self.assertEqual(scan_calls, [1] * BOUND)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
