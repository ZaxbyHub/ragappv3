"""Issue #515 acceptance tests — WikiCompileProcessor / commit-atomicity
defects (WIKI-010 commit, and the AC30 preserving baseline).

  AC11 (WIKI-010) An accepted curator claim + its source row must be
                   committed transactionally: after the compile/dispatch call
                   returns, a second pooled connection sees BOTH rows, the
                   store connection holds no open transaction, and the job
                   still completes. Today create_claim commits but
                   attach_source does not — the source insert only commits
                   incidentally via later lint writes.
  AC30 (PRESERVING) Zero claims with the curator disabled is not a compiler
                   failure — the job must complete successfully. This must
                   PASS at base; it pins behavior the other fixes must not
                   break.

The harness mirrors backend/tests/test_wiki_compile_processor.py
(SQLiteConnectionPool + run_migrations; the curator model is faked by
patching the WikiCurator constructor in the wiki_compiler namespace).
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (same as test_wiki_compile_processor.py).
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
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.wiki_store import WikiStore

INGEST_TEXT = "Justice Sakyi is the AFOMIS Chief."


def _seed_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
        conn.execute(
            "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, file_size, status) "
            "VALUES (1, 1, '/tmp/test.txt', 'test.txt', 100, 'indexed')"
        )
        conn.commit()
    finally:
        conn.close()


class _SettingsSnapshotMixin:
    _FIELDS = (
        "wiki_llm_curator_enabled",
        "wiki_llm_curator_run_on_ingest",
        "wiki_llm_curator_url",
        "wiki_llm_curator_model",
    )

    def _snapshot_settings(self):
        self._snap = {f: getattr(settings, f) for f in self._FIELDS}

    def _restore_settings(self):
        for f, v in self._snap.items():
            setattr(settings, f, v)


# ---------------------------------------------------------------------------
# AC11 (WIKI-010) — accepted curator claim+source commit transactionally
# ---------------------------------------------------------------------------


class TestIssue515Ac11CuratorCommitTransactional(unittest.TestCase, _SettingsSnapshotMixin):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.db = str(Path(self._td) / "test.db")
        run_migrations(self.db)
        _seed_db(self.db)
        self.pool = SQLiteConnectionPool(self.db, max_size=3)
        self._snapshot_settings()
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://api.example.com"
        settings.wiki_llm_curator_model = "qwen-1b"

    def tearDown(self):
        self.pool.close_all()
        self._restore_settings()
        import shutil
        shutil.rmtree(self._td, ignore_errors=True)

    def test_issue515_ac11_accepted_claim_and_source_commit_together(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_curator import CuratorAcceptedClaim, CuratorResult

        with self.pool.connection() as conn:
            job = WikiStore(conn).create_job(
                vault_id=1,
                trigger_type="ingest",
                input_json={"file_id": 1, "text": INGEST_TEXT},
            )

        async def _fake_curate(**_kwargs):
            result = CuratorResult()
            result.accepted = [
                CuratorAcceptedClaim(
                    claim_text="Curator verified fact about the chief role",
                    claim_type="fact",
                    subject=None,
                    predicate=None,
                    object=None,
                    source_quote=INGEST_TEXT,
                    chunk_id="1_0",
                    file_id=1,
                    source_label="file:1",
                    confidence=0.9,
                    status="active",
                )
            ]
            # ZERO lint findings — nothing after attach_source inside
            # _maybe_run_curator would commit the pending source insert.
            result.lint_findings = []
            result.errors = []
            return result

        proc = WikiCompileProcessor(self.pool)
        with patch("app.services.wiki_compiler.CuratorClient"), patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor:
            cur_ctor.return_value.curate = _fake_curate
            result = proc._dispatch(job)  # the compile/dispatch call

        self.assertFalse(result.get("skipped"))

        # (a) A SECOND connection to the same DB file sees the claim AND its
        # source row immediately after dispatch returns. The claim row is
        # committed by create_claim; the source row is not (attach_source
        # never commits), so today only the claim is visible.
        second = sqlite3.connect(self.db)
        try:
            claim_count = second.execute(
                "SELECT COUNT(*) FROM wiki_claims WHERE created_by_kind = 'llm_curator'"
            ).fetchone()[0]
            self.assertEqual(
                1, claim_count,
                "AC11 setup check: the accepted curator claim must be persisted",
            )
            source_count = second.execute(
                "SELECT COUNT(*) FROM wiki_claim_sources cs "
                "JOIN wiki_claims c ON c.id = cs.claim_id "
                "WHERE c.created_by_kind = 'llm_curator'"
            ).fetchone()[0]
            self.assertEqual(
                1, source_count,
                "AC11: the curator claim's source row must be committed together "
                "with the claim — a second connection must see it immediately "
                "after dispatch (attach_source left it uncommitted)",
            )
        finally:
            second.close()

        # (b) The store connection must hold no open transaction once the
        # compile/dispatch call returns. After dispatch, the connection was
        # released back to the pool; retrieving it yields that same object.
        store_conn = self.pool.get_connection()
        try:
            self.assertFalse(
                store_conn.in_transaction,
                "AC11: the store connection must not be returned with an open "
                "transaction after persisting an accepted curator claim",
            )
        finally:
            if store_conn.in_transaction:
                store_conn.rollback()
            self.pool.release_connection(store_conn)

        # (c) Job completion still succeeds end-to-end.
        proc._complete_job(job.id, result)
        with self.pool.connection() as conn:
            fetched = WikiStore(conn).get_job(job.id, vault_id=1)
        self.assertIsNotNone(fetched)
        self.assertEqual("completed", fetched.status)
        parsed = json.loads(fetched.result_json)
        self.assertIn("claims", parsed)
        self.assertIn("curator", parsed)


# ---------------------------------------------------------------------------
# AC30 (PRESERVING) — zero claims + curator disabled is not a failure
# ---------------------------------------------------------------------------


class TestIssue515Ac30ZeroClaimsCuratorDisabled(
    unittest.IsolatedAsyncioTestCase, _SettingsSnapshotMixin
):

    async def asyncSetUp(self):
        self._td = tempfile.mkdtemp()
        self.db = str(Path(self._td) / "test.db")
        run_migrations(self.db)
        _seed_db(self.db)
        self.pool = SQLiteConnectionPool(self.db, max_size=3)
        self._snapshot_settings()
        # Curator disabled — the processor's compile path must not even try
        # to construct a curator client, and zero extracted claims must not
        # fail the job.
        settings.wiki_llm_curator_enabled = False

    async def asyncTearDown(self):
        self.pool.close_all()
        self._restore_settings()
        import shutil
        shutil.rmtree(self._td, ignore_errors=True)

    async def test_issue515_ac30_zero_claims_curator_disabled_completes(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor

        plain_text = (
            "The product team shipped a release on Tuesday. "
            "Feedback was gathered from beta users and incorporated."
        )
        with self.pool.connection() as conn:
            WikiStore(conn).create_job(
                vault_id=1,
                trigger_type="ingest",
                input_json={"file_id": 1, "text": plain_text},
            )

        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        row = None
        try:
            for _ in range(100):  # up to 10 s
                await asyncio.sleep(0.1)
                with self.pool.connection() as conn:
                    row = conn.execute(
                        "SELECT status, result_json, error FROM wiki_compile_jobs "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                if row is not None and dict(row)["status"] in (
                    "completed", "failed", "cancelled"
                ):
                    break
        finally:
            await proc.stop()

        self.assertIsNotNone(row)
        self.assertEqual(
            "completed", dict(row)["status"],
            "AC30: an ingest job with zero extractable claims and the curator "
            "disabled must complete successfully, not fail",
        )
        result = json.loads(dict(row)["result_json"])
        self.assertIsInstance(result, dict)
        self.assertEqual([], result.get("claims"))
        self.assertFalse(result.get("skipped"))


if __name__ == "__main__":
    unittest.main()
