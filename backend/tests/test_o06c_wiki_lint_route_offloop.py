"""Off-loop check for the wiki lint run route (collated wiki lint
lifecycle trace, issues #929 + #932 + #935).

Fixture strategy mirrors backend/tests/test_k765_wiki_status_vocabulary.py:
a temp DB created through run_migrations, the vault row planted with plain
SQL, and the public route handler called directly with a permissive
evaluate — the same boundary production uses (a pooled
``check_same_thread=False`` connection handed to the coroutine).

C9 (AC9, DISCRIMINATING): ``run_wiki_lint`` calls the synchronous
``WikiLinter.run_lint`` inline on the event-loop thread, blocking every
other request on that loop for the whole scan (the same file already uses
``await asyncio.to_thread`` for its DB reads). The check stubs
``app.api.routes.wiki.WikiLinter`` so ``run_lint`` records the thread it
executes on and asserts it is NOT the thread running the event loop. RED
at base (same thread), GREEN after the fix.
"""

import shutil
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

_USER = {"id": 1, "username": "checker", "role": "superadmin", "is_active": True}


async def _allow_all(user, resource_type, resource_id, action):
    del user, resource_type, resource_id, action
    return True


def _connect(db_path):
    # check_same_thread=False: matches the production pool connections the
    # route handlers run against (and lets a fixed off-loop run_lint use
    # the connection from the executor thread).
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestRunWikiLintOffLoop(unittest.IsolatedAsyncioTestCase):
    # check: C9 (AC9, DISCRIMINATING) — RED at base, GREEN after the fix.

    async def asyncSetUp(self):
        from app.models.database import run_migrations

        self._tmp_root = Path(mkdtemp())
        self.db_path = str(self._tmp_root / "o06c-lint-route.db")
        run_migrations(self.db_path)
        self.conn = _connect(self.db_path)
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')"
        )
        self.conn.commit()

    async def asyncTearDown(self):
        self.conn.close()
        shutil.rmtree(self._tmp_root, ignore_errors=True)

    async def test_run_wiki_lint_executes_off_loop(self):
        from app.api.routes import wiki as wiki_routes
        from app.api.routes.wiki import LintRunRequest, run_wiki_lint

        recorded = {}

        class _StubLinter:
            def __init__(self, db, store):
                self._db = db
                self._store = store

            def run_lint(self, vault_id):
                recorded["thread_ident"] = threading.get_ident()
                recorded["called"] = True
                return []

        loop_thread = threading.get_ident()
        with patch.object(wiki_routes, "WikiLinter", _StubLinter):
            response = await run_wiki_lint(
                request=LintRunRequest(vault_id=1),
                db=self.conn,
                user=_USER,
                evaluate=_allow_all,
                _csrf_token="x",
            )

        # Anti-tautology guard: the stub's run_lint must actually have
        # executed through the route (otherwise the thread assert below
        # could pass vacuously on an unset recording).
        self.assertTrue(
            recorded.get("called"), "run_lint was never invoked by the route"
        )
        self.assertEqual(response["count"], 0)
        # Discriminating assertion: the synchronous lint scan must run on
        # a worker thread (asyncio.to_thread), not inline on the event
        # loop's thread. At base this fails with identical idents.
        self.assertNotEqual(
            recorded["thread_ident"], loop_thread,
            "run_wiki_lint executed WikiLinter.run_lint on the event-loop "
            "thread; the synchronous scan must be dispatched off-loop",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
