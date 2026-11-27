"""Shared fixtures for the o01 wiki-lint suites (issues #791 / #934).

Consolidates the environment bootstrap and curator-stub plumbing that
test_o01_wiki_lint_reconcile.py and test_o01_origin_guards.py previously
duplicated, and registers a cleanup for every ``mkdtemp`` it creates
(issue #934 item 6, pattern from backend/tests/test_schema_drift.py).
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models.database import run_migrations
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_curator import CuratorResult

CURATOR_FIELDS = (
    "wiki_llm_curator_enabled",
    "wiki_llm_curator_run_on_ingest",
    "wiki_llm_curator_run_on_query",
    "wiki_llm_curator_run_on_manual",
    "wiki_llm_curator_url",
    "wiki_llm_curator_model",
)


class DeterministicExtraction:
    """The deterministic-extractor duck type the compiler reads
    (``.acronyms`` / ``.persons`` / ``.role_claims``)."""

    acronyms: list = []
    persons: list = []
    role_claims: list = []


def make_env(test: unittest.TestCase) -> sqlite3.Connection:
    """A migrated connection on a temp database, removed on test exit."""
    td = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, td, ignore_errors=True)
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.commit()
    return conn


class CuratorHarness:
    """Curator settings snapshot + stubbed ``_maybe_run_curator`` plumbing.

    Snapshots the ``wiki_llm_curator_*`` settings on construction and
    restores them via the owning test's ``addCleanup``; each
    ``run_curator`` call enables the curator for ingest and drives
    ``WikiCompiler._maybe_run_curator`` against a stubbed
    ``WikiCurator.curate`` returning the supplied result.
    """

    def __init__(self, test: unittest.TestCase, conn: sqlite3.Connection, store):
        self._test = test
        self._conn = conn
        self._store = store
        self._settings = settings
        self._snap = {f: getattr(settings, f) for f in CURATOR_FIELDS}
        test.addCleanup(self._restore)
        compiler = WikiCompiler.__new__(WikiCompiler)  # type: ignore
        compiler._db = conn
        compiler._store = store
        self.compiler = compiler

    def _restore(self):
        for name, value in self._snap.items():
            setattr(self._settings, name, value)

    def enable(self):
        self._settings.wiki_llm_curator_enabled = True
        self._settings.wiki_llm_curator_run_on_ingest = True
        self._settings.wiki_llm_curator_url = "https://api.example.com"
        self._settings.wiki_llm_curator_model = "qwen-1b"

    @staticmethod
    def _curate(accepted, lint_findings):
        result = CuratorResult(
            accepted=list(accepted), lint_findings=list(lint_findings)
        )

        async def _run(**kwargs):
            return result

        return _run

    def run_curator(self, accepted, lint_findings=(), *, file_id: int):
        self.enable()
        with patch("app.services.wiki_compiler.CuratorClient"), patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor:
            cur_ctor.return_value.curate = self._curate(accepted, lint_findings)
            return self.compiler._maybe_run_curator(
                vault_id=1,
                file_id=file_id,
                text="probe text " * 50,
                trigger="ingest",
                deterministic_extraction=DeterministicExtraction(),
                page_id=None,
                existing_entities=[],
            )
