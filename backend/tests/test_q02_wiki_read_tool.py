"""Arm's-length acceptance checks for #804 (Workstream E tail, trace Q02 part 2).

Binds AC6-AC7 of .agents/issue-traces/804-805-workstream-e-tail/01-issue-summary.md:
a read-only WikiReadTool must exist in app.services.agentic_tools and must
gate page content on the vault-read policy check.

Frozen check contract (the implementer makes these green WITHOUT editing
this file):

- Module-level import: ``from app.services.agentic_tools import WikiReadTool``
  (this module fails to collect until the class exists - the expected base
  state is an import error / absent file).
- AC6: ``WikiReadTool(db, evaluate, user).name == "wiki_read"`` where db is a
  sqlite3 connection, evaluate an async callable
  ``(principal, resource_type, resource_id, action) -> bool`` and user the
  principal dict.
- AC7: ``await tool.execute(page_id=<seeded id>)`` returns a ToolResult whose
  output contains the page markdown when evaluate returns True, returns an
  error result WITHOUT the page markdown when evaluate returns False, and the
  evaluate call must be ``("vault", page.vault_id, "read")`` (policy before
  content).
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add backend/ and backend/tests/ to the import path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Settings() prerequisites when this module is imported outside conftest.
os.environ.setdefault("JWT_SECRET_KEY", "test-sa-wiki-reads-key-32chars-min!")
os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key")
os.environ.setdefault("REDIS_URL", "")

# Stub missing optional dependencies (same preamble as
# backend/tests/test_service_account_authenticates_to_routes.py).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.models.database import init_db, run_migrations  # noqa: E402

# NEW-SURFACE import: fails at base (ImportError) - that collection failure is
# the expected base evidence for this check file.
from app.services.agentic_tools import ToolResult, WikiReadTool  # noqa: E402


class TestWikiReadTool(unittest.IsolatedAsyncioTestCase):
    """AC6-AC7: WikiReadTool identity and vault-read policy gating."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")

        self._vault_id = 1
        self._page_id = 1
        self._page_markdown = "AC7 marker body 91bd for the wiki read tool."
        self._conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
            (self._vault_id, "Q02ToolVault"),
        )
        self._conn.execute(
            "INSERT INTO wiki_pages (id, vault_id, title, slug, page_type, status, markdown) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._page_id,
                self._vault_id,
                "Q02 Tool Page",
                "q02-tool-page",
                "overview",
                "verified",
                self._page_markdown,
            ),
        )
        self._conn.commit()
        self._principal = {
            "id": "sa:1",
            "role": "superadmin",
            "is_service_account": True,
            "is_active": True,
            "scopes": ["documents:read"],
            "name": "Q02-Read",
        }

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # AC6 ---------------------------------------------------------------

    async def test_wiki_read_tool_name(self):
        """AC6: WikiReadTool is importable and reports name 'wiki_read'."""

        async def allow(principal, resource_type, resource_id, action):
            return True

        tool = WikiReadTool(self._conn, allow, self._principal)
        self.assertEqual(
            tool.name,
            "wiki_read",
            f"AC6: WikiReadTool.name must be 'wiki_read', got {tool.name!r}",
        )

    # AC7 ---------------------------------------------------------------

    async def test_execute_gates_page_content_on_vault_read_policy(self):
        """AC7: execute invokes the vault-read policy and gates content on it.

        Allow leg: evaluate is called exactly once with
        ('vault', <vault_id>, 'read') and the ToolResult output contains the
        page markdown. Deny leg: no page markdown in the result and the
        result is an error result (success False or an error message).
        """
        allow_calls = []

        async def allow(principal, resource_type, resource_id, action):
            allow_calls.append((resource_type, resource_id, action))
            return True

        deny_calls = []

        async def deny(principal, resource_type, resource_id, action):
            deny_calls.append((resource_type, resource_id, action))
            return False

        allowed_tool = WikiReadTool(self._conn, allow, self._principal)
        result = await allowed_tool.execute(page_id=self._page_id)
        self.assertIsInstance(result, ToolResult, "AC7: execute must return a ToolResult")
        self.assertEqual(
            allow_calls,
            [("vault", self._vault_id, "read")],
            "AC7: the vault-read policy must be invoked with "
            f"('vault', {self._vault_id}, 'read') before content is returned; "
            f"observed: {allow_calls}",
        )
        self.assertIn(
            self._page_markdown,
            result.output,
            f"AC7: allowed execute must return the page markdown in the output; "
            f"got output={str(result.output)[:200]!r}",
        )

        denied_tool = WikiReadTool(self._conn, deny, self._principal)
        denied = await denied_tool.execute(page_id=self._page_id)
        self.assertEqual(
            deny_calls,
            [("vault", self._vault_id, "read")],
            f"AC7: deny leg must also invoke the policy; observed: {deny_calls}",
        )
        self.assertNotIn(
            self._page_markdown,
            denied.output or "",
            "AC7: denied execute must NOT return the page content",
        )
        self.assertTrue(
            denied.success is False or bool(denied.error),
            "AC7: denied execute must be an error result "
            f"(success=False or error set); got success={denied.success} "
            f"error={denied.error!r}",
        )


if __name__ == "__main__":
    unittest.main()
