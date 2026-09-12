"""Issue #494 acceptance check — AC40/PROMPT-001 (prompt store single-active).

Regression target: ``PromptVersionStore.create_version(activate=True)``
(backend/app/services/prompt_store.py:133-164) INSERTs the new row with
``is_active = 1`` but never deactivates any prior active row. The separate
``activate()`` method (:166-188) does the correct deactivate-then-activate
two-step; ``create_version`` bypasses that logic. The schema
(backend/app/models/database.py:1514-1522) has no partial unique constraint
on active rows, and ``get_active()`` (:31-39) selects ``WHERE is_active = 1
LIMIT 1`` with no ORDER BY — so after two ``create_version(...,
activate=True)`` calls there are TWO active rows and ``get_active()`` arms
the resolution on whichever row the scan hits first (v1, the lower rowid),
not the newly activated v2.

Expected at the pre-fix base (a543361): RED — the sentinel
``AC40 CHECK: FAIL`` prints immediately before the first discriminating
assertion. Green once ``create_version(activate=True)`` deactivates prior
active rows.
"""

import os
import sqlite3
import tempfile

from app.models.database import run_migrations
from app.services.prompt_store import PromptVersionStore


class TestIssue494Ac40PromptStore:
    def test_create_version_activate_deactivates_prior_active(self):
        """Two create_version(activate=True) calls leave exactly one active row (v2)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            run_migrations(path)
            conn = sqlite3.connect(path)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.row_factory = sqlite3.Row
                store = PromptVersionStore(conn)

                v1 = store.create_version(
                    "v1", "content of v1", activate=True, created_by="ac40"
                )
                assert v1.is_active is True  # sanity: v1 starts active

                v2 = store.create_version(
                    "v2", "content of v2", activate=True, created_by="ac40"
                )

                # (a) Exactly one row has is_active=1, and it is v2.
                print("AC40 CHECK: FAIL", flush=True)
                active_rows = conn.execute(
                    "SELECT id, version FROM prompt_versions"
                    " WHERE is_active = 1 ORDER BY id"
                ).fetchall()
                assert len(active_rows) == 1, (
                    f"expected exactly 1 active row after activating v2, "
                    f"found {len(active_rows)}: "
                    f"{[(r['id'], r['version']) for r in active_rows]}"
                )
                assert active_rows[0]["id"] == v2.id
                assert active_rows[0]["version"] == "v2"

                # v1 must have been deactivated by activating v2.
                v1_after = store.get_version("v1")
                assert v1_after is not None
                assert v1_after.is_active is False
                assert v1_after.id == v1.id

                # (b) get_active() returns the second (newly activated) version.
                active = store.get_active()
                assert active is not None, "get_active() returned None with v2 active"
                assert active.id == v2.id, (
                    f"get_active() returned id={active.id} "
                    f"({active.version!r}), expected id={v2.id} ('v2')"
                )
                assert active.version == "v2"
                assert active.content == "content of v2"
            finally:
                conn.close()
        finally:
            os.unlink(path)
