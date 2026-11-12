"""Review-round regression pins for the wave-6 runtime fixes (PR #1113,
swarm-pr-review pr1113-20261111). These live OUTSIDE the frozen check
files: they pin the two behavior changes the feedback round shipped after
the frozen checkpoint, each red on the pre-fix code.

- PRR-001: the lint scan runs on the DEDICATED bounded executor
  (``_WIKI_LINT_EXECUTOR``, thread_name_prefix='wiki-lint'), not on the
  shared default executor. RED if the dispatch reverts to bare
  ``asyncio.to_thread`` (default-executor worker threads do not carry the
  wiki-lint prefix), GREEN at head. This is the committed form of the
  review round's transient probe — without it, silently reverting the
  executor passed every durable check.
- PRR-002: ``_cap`` preserves ``None`` as ``None`` so absent optional
  contradiction fields persist as JSON null, exactly as they did before
  the cap (base stored raw ``c.get(...)`` values). RED if ``_cap`` goes
  back to ``str(value)`` (which stores a truthy literal "None" string),
  GREEN at head.
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

from app.config import settings  # noqa: E402
from app.models.database import run_migrations  # noqa: E402


def _make_env():
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, "
        "file_size, status) VALUES (1, 1, ?, 'test.txt', 100, 'indexed')",
        (str(Path(td) / "test.txt"),),
    )
    conn.commit()
    return conn, td


_FLAG_FIELDS = (
    "wiki_llm_curator_enabled",
    "wiki_llm_curator_run_on_ingest",
    "wiki_llm_curator_url",
    "wiki_llm_curator_model",
    "wiki_llm_curator_mode",
    "wiki_llm_curator_require_quote_match",
    "wiki_llm_curator_require_chunk_id",
    "wiki_llm_curator_run_on_manual",
)


class TestLintRunsOnDedicatedExecutor(unittest.TestCase):
    """PRR-001: the scan executes on a wiki-lint-prefixed worker thread."""

    def test_run_wiki_lint_uses_dedicated_executor_thread(self):
        from app.api.routes import wiki as routes

        conn, _td = _make_env()
        self.addCleanup(conn.close)
        recorded = {}

        class _StubLinter:
            def __init__(self, db, store):
                pass

            def run_lint(self, vault_id):
                recorded["thread_name"] = threading.current_thread().name
                return []

        import asyncio

        async def _evaluate(*_a, **_k):
            return True

        async def _call():
            return await routes.run_wiki_lint(
                request=types.SimpleNamespace(vault_id=1),
                db=conn,
                user={"id": 1},
                evaluate=_evaluate,
                _csrf_token="x",
            )

        with patch.object(routes, "WikiLinter", _StubLinter):
            response = asyncio.run(_call())
        self.assertEqual(response["count"], 0)
        # The scan must have actually run (harness guard).
        self.assertIn("thread_name", recorded)
        # Discriminating assertion: the dedicated executor's workers carry
        # the wiki-lint prefix. The shared default executor (bare
        # asyncio.to_thread) does not name its workers this way, so a
        # revert turns this red.
        self.assertTrue(
            recorded["thread_name"].startswith("wiki-lint"),
            f"run_lint must execute on the dedicated wiki-lint executor "
            f"(got worker thread {recorded['thread_name']!r} — a bare "
            f"asyncio.to_thread revert names default-pool workers "
            f"differently)",
        )


class TestCapPreservesNoneAsNull(unittest.TestCase):
    """PRR-002: absent optional contradiction fields persist as JSON null."""

    def test_absent_contradiction_field_persists_as_null(self):
        from app.services.wiki_curator import _cap

        # Unit-level discriminator first: _cap(None) must be None, not the
        # literal string "None" (the str()-only implementation returns
        # "None", which json.dumps stores as a truthy string).
        self.assertIsNone(
            _cap(None),
            "_cap must preserve None so absent fields persist as JSON "
            "null (str(None) would store a truthy 'None' string)",
        )
        # And the persisted shape through json.dumps matches base.
        payload = json.loads(json.dumps({"claim_a": _cap(None)}))
        self.assertIsNone(payload["claim_a"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
