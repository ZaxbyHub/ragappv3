"""Regression tests for issue #791 — Workstream O, PR 1.

Pins the accepted wiki-lint contract:
  - list_lint_findings orders by severity rank (critical, high,
    medium, low), not insertion order;
  - run_lint reconciliation is scoped to finding types the linter
    emits, so foreign findings stay open;
  - an auto-resolved finding reopens when its condition recurs;
  - curator reruns do not duplicate lint findings and keep per-file
    sources when a claim text repeats across files;
  - update_lint_finding on a missing id leaves no open transaction.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _make_env():
    from app.models.database import run_migrations
    from app.services.wiki_store import WikiStore

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.commit()
    store = WikiStore(conn)
    return conn, store


class _SeverityOrder:
    """An expected severity sequence.

    Equality and repr match a plain list of severities; the dedicated
    type keeps pytest from abbreviating either operand in the failure
    line, so the observed order prints in full.
    """

    def __init__(self, severities):
        self._severities = list(severities)

    def __repr__(self):
        return repr(self._severities)

    def __eq__(self, other):
        if isinstance(other, list):
            return self._severities == other
        return NotImplemented

    __hash__ = None


class TestLintFindingsSeverityOrder(unittest.TestCase):
    """list_lint_findings returns findings ordered by severity rank."""

    def setUp(self):
        self.conn, self.store = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_list_lint_findings_orders_by_severity_rank(self):
        for sev in ("low", "critical", "high", "medium"):
            self.store.create_lint_finding(
                vault_id=1, finding_type="orphan", title=f"t-{sev}", severity=sev
            )
        order = [f.severity for f in self.store.list_lint_findings(1)]
        expected = _SeverityOrder(["critical", "high", "medium", "low"])
        assert order == expected


class TestLintReconcileScope(unittest.TestCase):
    """run_lint reconciles only finding types the linter itself emits."""

    def setUp(self):
        self.conn, self.store = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_run_lint_leaves_non_linter_finding_open(self):
        from app.services.wiki_linter import WikiLinter

        self.store.create_lint_finding(
            vault_id=1,
            finding_type="stale",
            title="Claim stale: source document deleted (file_id=7)",
            severity="medium",
        )
        WikiLinter(self.conn, self.store).run_lint(1)
        row_status = self.conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE finding_type='stale'"
        ).fetchone()["status"]
        assert row_status == "open"

    def test_auto_resolved_finding_reopens_on_recurrence(self):
        from app.services.wiki_linter import WikiLinter

        linter = WikiLinter(self.conn, self.store)
        claim = self.store.create_claim(
            vault_id=1, claim_text="Recurrence probe claim", source_type="manual"
        )
        linter.run_lint(1)
        self.store.attach_source(
            claim_id=claim.id, source_kind="document", file_id=1, chunk_id="1_0"
        )
        self.conn.commit()
        linter.run_lint(1)
        self.conn.execute(
            "DELETE FROM wiki_claim_sources WHERE claim_id = ?", (claim.id,)
        )
        self.conn.commit()
        linter.run_lint(1)
        open_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings WHERE vault_id=1 "
            "AND status='open' AND finding_type='unsupported_claim'"
        ).fetchone()[0]
        assert open_count == 1


_CURATOR_FIELDS = (
    "wiki_llm_curator_enabled",
    "wiki_llm_curator_run_on_ingest",
    "wiki_llm_curator_run_on_query",
    "wiki_llm_curator_run_on_manual",
    "wiki_llm_curator_url",
    "wiki_llm_curator_model",
)


class TestCuratorFindingPersistence(unittest.TestCase):
    """Curator reruns dedupe lint findings and keep per-file sources."""

    def setUp(self):
        self.conn, self.store = _make_env()
        from app.config import settings

        self._settings = settings
        self._snap = {f: getattr(settings, f) for f in _CURATOR_FIELDS}

    def tearDown(self):
        for name, value in self._snap.items():
            setattr(self._settings, name, value)
        self.conn.close()

    def _compiler(self):
        from app.services.wiki_compiler import WikiCompiler

        compiler = WikiCompiler.__new__(WikiCompiler)  # type: ignore
        compiler._db = self.conn
        compiler._store = self.store
        return compiler

    def _ext(self):
        # The deterministic extractor duck-type: the compiler only reads
        # .acronyms / .persons / .role_claims.
        class _E:
            acronyms: list = []
            persons: list = []
            role_claims: list = []

        return _E()

    def _enable(self):
        self._settings.wiki_llm_curator_enabled = True
        self._settings.wiki_llm_curator_run_on_ingest = True
        self._settings.wiki_llm_curator_url = "https://api.example.com"
        self._settings.wiki_llm_curator_model = "qwen-1b"

    def _curate(self, accepted, lint_findings):
        from app.services.wiki_curator import CuratorResult

        result = CuratorResult(
            accepted=list(accepted), lint_findings=list(lint_findings)
        )

        async def _run(**kwargs):
            return result

        return _run

    def _run_curator(self, compiler, *, file_id):
        compiler._maybe_run_curator(
            vault_id=1,
            file_id=file_id,
            text="probe text " * 50,
            trigger="ingest",
            deterministic_extraction=self._ext(),
            page_id=None,
            existing_entities=[],
        )

    def test_curator_rerun_does_not_duplicate_lint_finding(self):
        finding = {
            "finding_type": "contradiction",
            "severity": "high",
            "title": "Curator contradiction probe",
            "details": "d",
        }
        compiler = self._compiler()
        self._enable()
        with patch("app.services.wiki_compiler.CuratorClient"), patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor:
            cur_ctor.return_value.curate = self._curate([], [finding])
            for _ in range(2):
                self._run_curator(compiler, file_id=1)
        open_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings WHERE vault_id=1 "
            "AND status='open' AND title='Curator contradiction probe'"
        ).fetchone()[0]
        assert open_count == 1

    def test_curator_duplicate_claim_text_keeps_second_source(self):
        from app.services.wiki_curator import CuratorAcceptedClaim

        def _acc(file_id):
            return CuratorAcceptedClaim(
                claim_text="Duplicate claim text probe",
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

        compiler = self._compiler()
        self._enable()
        with patch("app.services.wiki_compiler.CuratorClient"), patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor:
            cur_ctor.return_value.curate = self._curate([_acc(1)], [])
            self._run_curator(compiler, file_id=1)
            cur_ctor.return_value.curate = self._curate([_acc(2)], [])
            self._run_curator(compiler, file_id=2)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_claim_sources cs JOIN wiki_claims c "
            "ON c.id = cs.claim_id WHERE c.vault_id=1 AND "
            "c.claim_text='Duplicate claim text probe'"
        ).fetchone()[0]
        assert count == 2


class TestUpdateLintFindingTransaction(unittest.TestCase):
    """update_lint_finding on a missing id leaves no open transaction."""

    def setUp(self):
        self.conn, self.store = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_update_lint_finding_not_found_leaves_no_open_transaction(self):
        res = self.store.update_lint_finding(99999, 1, "resolved")
        assert res is None
        assert self.conn.in_transaction is False
