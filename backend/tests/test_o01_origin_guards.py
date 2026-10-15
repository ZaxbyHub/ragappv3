"""Origin and suppression guards for issue #791 (Workstream O, PR 1).

Each guard names the WRONG implementation it kills — the frozen checks
C1-C10 cannot distinguish the shipped fix from the rejected "minimal"
candidate (finding_type-scoped resolve pass + suppression that excludes all
'resolved' rows, no origin column):

(i)   curator/compiler rows carrying LINTER-emitted finding_types survive
      run_lint — kills finding_type scoping;
(ii)  user-resolved and user-acknowledged findings stay suppressed on
      re-detection — kills suppression-excludes-resolved;
(iii) a mixed [auto_resolved, dismissed] fingerprint stays suppressed —
      pins the precedence rule that a user disposition always wins;
(iv)  a curator rerun of the SAME file attaches no duplicate source row —
      pins the source-dedupe semantics;
(v)   a detect -> undetect -> re-detect cycle holds exactly one row per
      fingerprint — pins the reopen-by-flip design (no per-flap growth).
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models.database import run_migrations
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_curator import CuratorAcceptedClaim, CuratorResult
from app.services.wiki_linter import WikiLinter
from app.services.wiki_store import WikiStore


def _make_env():
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.commit()
    return conn, WikiStore(conn)


_CURATOR_FIELDS = (
    "wiki_llm_curator_enabled",
    "wiki_llm_curator_run_on_ingest",
    "wiki_llm_curator_run_on_query",
    "wiki_llm_curator_run_on_manual",
    "wiki_llm_curator_url",
    "wiki_llm_curator_model",
)


class _Ext:
    acronyms: list = []
    persons: list = []
    role_claims: list = []


class TestOriginScoping(unittest.TestCase):
    """Guard (i): non-linter producers survive run_lint even when their
    finding_type is one the linter itself emits."""

    def setUp(self):
        self.conn, self.store = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_curator_origin_row_with_linter_type_survives_run_lint(self):
        self.store.upsert_lint_findings(
            1,
            [{"finding_type": "contradiction", "severity": "high",
              "title": "Curator contradiction probe", "details": ""}],
            origin="curator",
        )
        WikiLinter(self.conn, self.store).run_lint(1)
        row = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE title='Curator contradiction probe'"
        ).fetchone()
        self.assertEqual(row["status"], "open")

    def test_compiler_origin_row_with_linter_type_survives_run_lint(self):
        self.store.upsert_lint_findings(
            1,
            [{"finding_type": "unsupported_claim", "severity": "low",
              "title": "Unverified claim: extracted from query answer probe",
              "details": ""}],
            origin="compiler",
        )
        WikiLinter(self.conn, self.store).run_lint(1)
        row = self.conn.execute(
            "SELECT status FROM wiki_lint_findings"
            " WHERE title='Unverified claim: extracted from query answer probe'"
        ).fetchone()
        self.assertEqual(row["status"], "open")

    def test_linter_origin_row_still_auto_resolves_when_undetected(self):
        self.store.upsert_lint_findings(
            1,
            [{"finding_type": "contradiction", "severity": "high",
              "title": "Linter-owned probe", "details": ""}],
            origin="linter",
        )
        WikiLinter(self.conn, self.store).run_lint(1)
        row = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE title='Linter-owned probe'"
        ).fetchone()
        self.assertEqual(row["status"], "auto_resolved")


class TestUserTerminalSuppression(unittest.TestCase):
    """Guard (ii) + (iii): user dispositions suppress re-detection, even
    alongside auto_resolved siblings."""

    def setUp(self):
        self.conn, self.store = _make_env()
        self.linter = WikiLinter(self.conn, self.store)
        claim = self.store.create_claim(
            vault_id=1, claim_text="Suppression probe claim", source_type="manual"
        )
        self.claim = claim
        self.linter.run_lint(1)
        self.finding_id = self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE finding_type='unsupported_claim'"
        ).fetchone()["id"]

    def tearDown(self):
        self.conn.close()

    def _reseed_open_and_dispose(self, status):
        # The finding is currently open; dispose of it as a user would, then
        # re-run lint with the claim still unsupported (re-detection).
        self.store.update_lint_finding(self.finding_id, 1, status)
        self.linter.run_lint(1)
        rows = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE finding_type='unsupported_claim'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], status)

    def test_user_resolved_stays_suppressed(self):
        self._reseed_open_and_dispose("resolved")

    def test_user_acknowledged_stays_suppressed(self):
        self._reseed_open_and_dispose("acknowledged")

    def test_mixed_auto_resolved_and_dismissed_stays_suppressed(self):
        # Cycle once: auto-resolve (attach + detach a source), then dismiss
        # the reopened row, then recur again — rule 2 must win.
        self.store.attach_source(
            claim_id=self.claim.id, source_kind="document", file_id=1, chunk_id="1_0"
        )
        self.conn.commit()
        self.linter.run_lint(1)  # auto-resolves
        self.conn.execute("DELETE FROM wiki_claim_sources WHERE claim_id = ?",
                          (self.claim.id,))
        self.conn.commit()
        self.linter.run_lint(1)  # reopens (flip)
        reopened = self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE finding_type='unsupported_claim'"
            " AND status='open'"
        ).fetchone()
        self.assertIsNotNone(reopened)
        self.store.update_lint_finding(reopened["id"], 1, "dismissed")
        # Recur a second time: the dismissed row must suppress everything.
        self.store.attach_source(
            claim_id=self.claim.id, source_kind="document", file_id=2, chunk_id="2_0"
        )
        self.conn.commit()
        self.linter.run_lint(1)
        self.conn.execute("DELETE FROM wiki_claim_sources WHERE claim_id = ?",
                          (self.claim.id,))
        self.conn.commit()
        self.linter.run_lint(1)
        rows = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE finding_type='unsupported_claim'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "dismissed")


class TestCuratorSourceDedupe(unittest.TestCase):
    """Guard (iv) + (v): same-file reruns attach no duplicate source; the
    detect cycle holds one row per fingerprint."""

    def setUp(self):
        self.conn, self.store = _make_env()
        self._settings = settings
        self._snap = {f: getattr(settings, f) for f in _CURATOR_FIELDS}
        compiler = WikiCompiler.__new__(WikiCompiler)
        compiler._db = self.conn
        compiler._store = self.store
        self.compiler = compiler

    def tearDown(self):
        for name, value in self._snap.items():
            setattr(self._settings, name, value)
        self.conn.close()

    def _run_curator(self, accepted, file_id):
        result = CuratorResult(accepted=list(accepted), lint_findings=[])

        async def _curate(**kwargs):
            return result

        self._settings.wiki_llm_curator_enabled = True
        self._settings.wiki_llm_curator_run_on_ingest = True
        self._settings.wiki_llm_curator_url = "https://api.example.com"
        self._settings.wiki_llm_curator_model = "qwen-1b"
        with patch("app.services.wiki_compiler.CuratorClient"), patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor:
            cur_ctor.return_value.curate = _curate
            return self.compiler._maybe_run_curator(
                vault_id=1,
                file_id=file_id,
                text="probe text " * 50,
                trigger="ingest",
                deterministic_extraction=_Ext(),
                page_id=None,
                existing_entities=[],
            )

    @staticmethod
    def _accepted(file_id):
        return CuratorAcceptedClaim(
            claim_text="Dedupe probe claim",
            claim_type="fact",
            subject=None,
            predicate=None,
            object=None,
            source_quote="q",
            chunk_id=f"{file_id}_0",
            file_id=file_id,
            source_label=f"file:{file_id}",
            confidence=0.9,
            status="needs_review",
        )

    def test_same_file_rerun_attaches_no_duplicate_source(self):
        self._run_curator([self._accepted(1)], file_id=1)
        self._run_curator([self._accepted(1)], file_id=1)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claim_sources cs JOIN wiki_claims c"
            " ON c.id = cs.claim_id WHERE c.vault_id=1 AND c.claim_text='Dedupe probe claim'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_flip_cycle_holds_one_row_per_fingerprint(self):
        linter = WikiLinter(self.conn, self.store)
        claim = self.store.create_claim(
            vault_id=1, claim_text="Flip cycle claim", source_type="manual"
        )
        for cycle in range(3):
            linter.run_lint(1)  # detect (unsupported)
            self.store.attach_source(
                claim_id=claim.id, source_kind="document",
                file_id=cycle + 1, chunk_id=f"{cycle}_0",
            )
            self.conn.commit()
            linter.run_lint(1)  # undetect (auto-resolve)
            self.conn.execute(
                "DELETE FROM wiki_claim_sources WHERE claim_id = ?", (claim.id,)
            )
            self.conn.commit()
        linter.run_lint(1)  # final re-detection
        rows = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE finding_type='unsupported_claim'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "open")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
