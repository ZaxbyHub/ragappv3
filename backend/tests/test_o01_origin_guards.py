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

swarm-pr-review round (pr930-20261015) additions:
(vi)   the mixed state is SEEDED directly (two same-fingerprint rows, one
       dismissed and one auto_resolved) so the precedence rule is actually
       exercised — the original guard (iii) only ever held one row;
(vii)  update_lint_finding rejects the system-only 'auto_resolved' status;
(viii) curator lint findings carry a per-instance discriminator, so two
       DIFFERENT claims in one rejection class are two rows and dismissing
       one does not silence the other;
(ix)   the flip refreshes the mutable payload (severity/details/related ids)
       from the new detection;
(x)    a non-integer related_page_ids element cannot abort the whole
       reconciliation.
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models.database import run_migrations
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_curator import (
    CuratorAcceptedClaim,
    CuratorChunk,
    CuratorResult,
    WikiCurator,
)
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

    def test_seeded_mixed_state_suppresses_and_reordering_rule3_first_fails(self):
        """PRR-008 (swarm-pr-review round): construct the mixed state the
        original guard only described — two rows sharing one fingerprint,
        one dismissed and one auto_resolved — and pin that rule 2 wins.

        A mutation that evaluates rule 3 (flip) before rule 2 (suppress)
        reopens the auto_resolved sibling here, so this guard is
        discriminating for the precedence itself.
        """
        now = "2026-10-15T12:00:00.000000"
        # Same fingerprint: identical finding_type + normalized title + pages.
        self.conn.execute(
            "INSERT INTO wiki_lint_findings (vault_id, finding_type, severity,"
            " title, details, related_page_ids_json, related_claim_ids_json,"
            " status, origin, created_at, updated_at)"
            " VALUES (1, 'unsupported_claim', 'medium', 'Mixed state probe', '',"
            " '[]', '[]', 'dismissed', 'linter', ?, ?)",
            (now, now),
        )
        self.conn.execute(
            "INSERT INTO wiki_lint_findings (vault_id, finding_type, severity,"
            " title, details, related_page_ids_json, related_claim_ids_json,"
            " status, origin, created_at, updated_at)"
            " VALUES (1, 'unsupported_claim', 'medium', 'mixed   STATE probe', '',"
            " '[]', '[]', 'auto_resolved', 'linter', ?, ?)",
            (now, now),
        )
        self.conn.commit()
        specs = [
            {
                "finding_type": "unsupported_claim",
                "severity": "medium",
                "title": "Mixed state probe",
                "details": "",
                "related_page_ids": [],
                "related_claim_ids": [],
            }
        ]
        result = self.store.upsert_lint_findings(1, specs, origin="linter")
        # Rule 2 must suppress: nothing returned, nothing flipped open.
        self.assertEqual(result, [])
        rows = self.conn.execute(
            "SELECT status FROM wiki_lint_findings"
            " WHERE finding_type='unsupported_claim'"
            " AND title IN ('Mixed state probe', 'mixed   STATE probe')"
            " ORDER BY id"
        ).fetchall()
        self.assertEqual([r["status"] for r in rows], ["dismissed", "auto_resolved"])

    def test_update_lint_finding_rejects_system_only_status(self):
        """PRR-009: 'auto_resolved' is reconciler-owned; the user-settable set
        is unchanged, so a client cannot mint the system status."""
        self.store.create_lint_finding(
            vault_id=1, finding_type="orphan", title="System-only probe"
        )
        row = self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE title='System-only probe'"
        ).fetchone()
        with self.assertRaises(ValueError):
            self.store.update_lint_finding(row["id"], 1, "auto_resolved")
        # The row is untouched and the connection is usable.
        status = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE id = ?", (row["id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "open")
        self.assertFalse(self.conn.in_transaction)

    def test_flip_refreshes_mutable_payload_from_new_detection(self):
        """PRR-005: the fingerprint does not cover severity/details, so a
        re-detection with a changed payload must not keep the stale one."""
        claim = self.store.create_claim(
            vault_id=1, claim_text="Payload refresh probe", source_type="manual"
        )
        self.linter.run_lint(1)
        self.store.attach_source(
            claim_id=claim.id, source_kind="document", file_id=1, chunk_id="1_0"
        )
        self.conn.commit()
        self.linter.run_lint(1)  # auto-resolves
        row_id = self.conn.execute(
            "SELECT id, details FROM wiki_lint_findings"
            " WHERE finding_type='unsupported_claim'"
        ).fetchone()["id"]
        # Mutate the payload the way a changed detection would (identity is
        # unchanged: same type + normalized title + page ids).
        self.conn.execute(
            "UPDATE wiki_lint_findings SET details = 'stale payload' WHERE id = ?",
            (row_id,),
        )
        self.conn.commit()
        self.conn.execute("DELETE FROM wiki_claim_sources WHERE claim_id = ?",
                          (claim.id,))
        self.conn.commit()
        self.linter.run_lint(1)  # recurrence -> flip
        details = self.conn.execute(
            "SELECT details FROM wiki_lint_findings WHERE id = ?", (row_id,)
        ).fetchone()["details"]
        self.assertNotEqual(details, "stale payload")

    def test_kept_open_row_refreshes_payload_only_when_it_differs(self):
        """PRR-005 (review round): rule 1 keeps the row but must refresh the
        mutable payload when the new detection differs — and must not write
        when it does not (no per-run write amplification)."""
        claim = self.store.create_claim(
            vault_id=1, claim_text="Keep refresh probe", source_type="manual"
        )
        self.linter.run_lint(1)
        row_id = self.conn.execute(
            "SELECT id, updated_at FROM wiki_lint_findings"
            " WHERE finding_type='unsupported_claim'"
        ).fetchone()["id"]

        # 1. Same payload again: no write (updated_at unchanged).
        before = self.conn.execute(
            "SELECT updated_at FROM wiki_lint_findings WHERE id = ?", (row_id,)
        ).fetchone()["updated_at"]
        self.linter.run_lint(1)
        after_same = self.conn.execute(
            "SELECT updated_at FROM wiki_lint_findings WHERE id = ?", (row_id,)
        ).fetchone()["updated_at"]
        self.assertEqual(before, after_same)

        # 2. Changed payload (details is not part of the fingerprint): the
        #    stored row is refreshed from the new detection.
        self.conn.execute(
            "UPDATE wiki_lint_findings SET details = 'stale keep payload'"
            " WHERE id = ?",
            (row_id,),
        )
        self.conn.commit()
        self.linter.run_lint(1)
        details = self.conn.execute(
            "SELECT details FROM wiki_lint_findings WHERE id = ?", (row_id,)
        ).fetchone()["details"]
        self.assertNotEqual(details, "stale keep payload")
        # Identity is untouched: same row id, same title, still open.
        row = self.conn.execute(
            "SELECT id, title, status FROM wiki_lint_findings WHERE id = ?",
            (row_id,),
        ).fetchone()
        self.assertEqual(row["id"], row_id)
        self.assertEqual(row["status"], "open")

    def test_non_integer_page_id_cannot_abort_reconciliation(self):
        """PRR-023: the CHECK constrains related_page_ids_json to a JSON array,
        not to integers; one malformed legacy row must not abort the run."""
        self.conn.execute(
            "INSERT INTO wiki_lint_findings (vault_id, finding_type, severity,"
            " title, related_page_ids_json, related_claim_ids_json, status)"
            " VALUES (1, 'orphan', 'low', 'Malformed page id probe',"
            " '[\"abc\"]', '[]', 'open')"
        )
        self.conn.commit()
        # Must not raise (a ValueError here would 500 the lint route).
        result = self.store.upsert_lint_findings(
            1,
            [{"finding_type": "orphan", "severity": "low",
              "title": "Fresh probe", "details": ""}],
            origin="linter",
        )
        self.assertEqual(len(result), 1)
        statuses = {
            r["status"]
            for r in self.conn.execute(
                "SELECT status FROM wiki_lint_findings"
            ).fetchall()
        }
        self.assertIn("auto_resolved", statuses)


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


class TestCuratorFindingIdentity(unittest.TestCase):
    """PRR-001 (swarm-pr-review round): curator lint findings must carry a
    per-instance discriminator, so two DIFFERENT claims in one rejection class
    are two rows — and dismissing one does not silence the other."""

    class _StubClient:
        def __init__(self, payload: str):
            self.payload = payload

        async def propose(self, *_args, **_kwargs):
            return self.payload

    def setUp(self):
        self.conn, self.store = _make_env()
        self._settings = settings
        self._snap = {
            f: getattr(settings, f)
            for f in (
                "wiki_llm_curator_enabled",
                "wiki_llm_curator_url",
                "wiki_llm_curator_model",
                "wiki_llm_curator_mode",
                "wiki_llm_curator_require_quote_match",
                "wiki_llm_curator_require_chunk_id",
            )
        }
        self._settings.wiki_llm_curator_enabled = True
        self._settings.wiki_llm_curator_url = "https://api.example.com"
        self._settings.wiki_llm_curator_model = "qwen-1b"
        self._settings.wiki_llm_curator_mode = "active_if_verified"
        self._settings.wiki_llm_curator_require_quote_match = True
        self._settings.wiki_llm_curator_require_chunk_id = True

    def tearDown(self):
        for name, value in self._snap.items():
            setattr(self._settings, name, value)
        self.conn.close()

    @staticmethod
    def _chunks():
        return [
            CuratorChunk(
                chunk_id="42_0",
                source_text="Alice founded the company. Bob is the CTO.",
                file_id=42,
                source_label="file:42",
            )
        ]

    def _curate(self, payload: str) -> CuratorResult:
        curator = WikiCurator(
            client=self._StubClient(payload), store=self.store
        )
        return asyncio.run(
            curator.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )

    def test_two_rejections_in_one_class_are_two_distinct_findings(self):
        payload = json.dumps(
            {
                "claims": [
                    {"claim_text": "First unsupported assertion",
                     "source_quote": "", "chunk_id": "42_0"},
                    {"claim_text": "Second unsupported assertion",
                     "source_quote": "", "chunk_id": "42_0"},
                ]
            }
        )
        result = self._curate(payload)
        titles = [f["title"] for f in result.lint_findings]
        self.assertEqual(len(titles), 2)
        self.assertNotEqual(titles[0], titles[1])
        self.assertIn("First unsupported assertion", titles[0])
        self.assertIn("Second unsupported assertion", titles[1])

    def test_dismissing_one_claim_does_not_silence_the_class(self):
        """The regression this class of bug caused: with constant titles the
        two claims shared one fingerprint, so dismissing either suppressed
        both — permanently and silently."""
        payload = json.dumps(
            {
                "claims": [
                    {"claim_text": "Alpha unsupported assertion",
                     "source_quote": "", "chunk_id": "42_0"},
                    {"claim_text": "Beta unsupported assertion",
                     "source_quote": "", "chunk_id": "42_0"},
                ]
            }
        )
        result = self._curate(payload)
        specs = [
            {
                "finding_type": f["finding_type"],
                "severity": f.get("severity", "medium"),
                "title": f["title"],
                "details": f.get("details", ""),
                "related_page_ids": f.get("related_page_ids", []),
                "related_claim_ids": f.get("related_claim_ids", []),
            }
            for f in result.lint_findings
        ]
        self.store.upsert_lint_findings(1, specs, origin="curator")
        rows = self.conn.execute(
            "SELECT id, title FROM wiki_lint_findings"
            " WHERE finding_type='unsupported_claim' AND status='open'"
            " ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)

        # Dismiss the first claim's row, then re-detect BOTH claims: the
        # second must still be reported (and the first stay suppressed).
        self.store.update_lint_finding(rows[0]["id"], 1, "dismissed")
        again = self.store.upsert_lint_findings(1, specs, origin="curator")
        self.assertEqual([f.id for f in again], [rows[1]["id"]])
        dismissed = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE id = ?", (rows[0]["id"],)
        ).fetchone()["status"]
        self.assertEqual(dismissed, "dismissed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
