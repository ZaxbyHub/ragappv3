"""Pin for the lint_only defensive guard (issue #1162, review finding A).

The suppressed-ingest call passes lint_only=True so accepted claims can
never persist without a page. In the production flow accepted>0 implies
qualifying (the branch is unreachable with claims), so this pin exercises
the helper directly: a CuratorResult carrying accepted claims persists
NOTHING claim-shaped under lint_only=True and persists the claims under
the default mode.
"""

import tempfile
import unittest
from pathlib import Path

from app.models.database import init_db, run_migrations
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_curator import CuratorAcceptedClaim, CuratorResult
from app.services.wiki_store import WikiStore


def _mkdtemp_conn():
    import sqlite3

    db_path = str(Path(tempfile.mkdtemp(prefix="lint-only-")) / "lo.db")
    init_db(db_path)
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    return conn


def _cur_result():
    cur = CuratorResult()
    cur.accepted.append(
        CuratorAcceptedClaim(
            claim_text="A claim that must not persist without a page.",
            claim_type="fact",
            subject="AFOMIS",
            predicate="states",
            object="something",
            source_quote="A claim that must not persist without a page.",
            chunk_id="1_0",
            file_id=1,
            source_label="file:1",
            confidence=0.9,
            status="needs_review",
        )
    )
    return cur


class TestLintOnlyGuard(unittest.TestCase):
    def setUp(self):
        self.conn = _mkdtemp_conn()
        self.addCleanup(self.conn.close)
        self.store = WikiStore(self.conn)
        self.compiler = WikiCompiler(self.conn)

    def _claims(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()[0]

    def test_lint_only_never_persists_accepted_claims(self):
        self.compiler._persist_curator_result(
            _cur_result(), vault_id=1, page_id=None, lint_only=True
        )
        self.conn.commit()
        self.assertEqual(self._claims(), 0)

    def test_default_mode_persists_accepted_claims(self):
        self.compiler._persist_curator_result(
            _cur_result(), vault_id=1, page_id=None
        )
        self.conn.commit()
        self.assertEqual(self._claims(), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
