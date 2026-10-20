"""Issue #957 acceptance checks: ingestion and legacy-reindex failure
settle boundaries must persist redacted stable codes, never raw
``str(exc)`` — while the documented ``ReindexOperatorGuidance`` carve-out
and the lease-path reindex redaction stay untouched.

The lease-path reindex runner already demonstrates the correct boundary
(``redact_ingest_error(exc)`` with a ``ReindexOperatorGuidance`` verbatim
carve-out). The unredacted members of the same family:

- ``_process_ingest_job_row`` sets ``outcome_error = str(exc)`` and forwards
  it into the requeue settle and the ``attempt_cap_exceeded: {raw}`` cap
  literal (``jobs.error``);
- ``_process_reindex_job`` (legacy path) persists ``str(exc)`` /
  ``str(error)`` verbatim into ``document_reindex_jobs.error``.

Checks:

- AC10: an ingestion job whose processing raises (``_run_task_processing``
  patched to raise a sentinel-bearing RuntimeError) settles with a
  ``jobs.error`` that does NOT contain the sentinel — for the requeue arm
  (``jobs_max_attempts=3``) and the attempt-cap arm
  (``jobs_max_attempts=1``, the error may carry an
  ``attempt_cap_exceeded`` code but never the sentinel).
- AC11: both arms of the legacy reindex path — ``_reindex_embed_all``
  raising (catch-all arm) and returning
  ``("failed", {}, "raw text with sentinel")`` (returned-error arm) —
  persist a ``document_reindex_jobs.error`` that does NOT contain the
  sentinel.
- AC11 PRESERVING (behavioral): a raised ``ReindexOperatorGuidance``
  (hand-written operator guidance) IS persisted verbatim — ``str()`` of it,
  byte-identical — the documented carve-out.
- AC11 PRESERVING (source shape): ``_run_reindex_job_row`` still carries
  the ``redact_ingest_error`` call with the ``ReindexOperatorGuidance``
  isinstance carve-out (the lease-path boundary is untouched).
- AC12 (PRESERVING) is covered by the existing
  ``backend/tests/test_issue562_guardrail_no_raw_ingest_error_persist.py``
  staying green — its logic is deliberately not duplicated here.

RED at base for the AC10/AC11 discriminating checks (the raw sentinel is
persisted verbatim); GREEN after the fix. The PRESERVING checks are GREEN
at base and must stay GREEN.

Harness: temp ``run_migrations`` DBs, JobLease-claimed ingestion rows,
``BackgroundProcessor._process_ingest_job_row`` / ``_process_reindex_job``
awaited directly with only the seam under test patched (mirrors the frozen
o02 suites and the Phase-2 probes).
"""

import ast
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN", "o02f-token-0123456789abcdef0123456789abcdef"
)

from app.config import settings  # noqa: E402

INGEST_SENTINEL = "O02F-SENTINEL-INGEST-RAW pw=hunter2"
REINDEX_EXC_SENTINEL = "O02F-SENTINEL-REINDEX-EXC-RAW token=sk-1"
REINDEX_RET_SENTINEL = "O02F-SENTINEL-REINDEX-RET-RAW v2=pw"
GUIDANCE_TEXT = (
    "O02F-GUIDANCE-KEEP: model identity changed mid-reindex; run a full"
    " reindex to rebuild the index"
)

BACKGROUND_TASKS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "services",
    "background_tasks.py",
)


def _mkdb(prefix: str) -> str:
    from app.models.database import run_migrations

    tmp_dir = mkdtemp(prefix=prefix)
    db_path = str(Path(tmp_dir) / "o02f.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    conn.close()
    return db_path


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class TestIngestionErrorRedaction(unittest.IsolatedAsyncioTestCase):
    """AC10 (#957): ingestion requeue / attempt-cap error redaction."""

    def setUp(self):
        self.tmp_dirs = []

    def _db(self, prefix: str) -> str:
        db_path = _mkdb(prefix)
        self.tmp_dirs.append(os.path.dirname(db_path))
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        return db_path

    async def _drive_ingest_failure(self, max_attempts: int):
        from app.models.database import SQLiteConnectionPool
        from app.services.background_tasks import (
            INGESTION_QUEUE,
            BackgroundProcessor,
        )
        from app.services.job_lease import JobLease, ensure_jobs_schema

        db_path = self._db(f"o02f-ingest-{max_attempts}-")
        conn = _conn(db_path)
        try:
            ensure_jobs_schema(conn)
            conn.execute(
                "INSERT INTO jobs (queue, payload_json, status) VALUES "
                "(?, json_object('file_path', '/tmp/o02f-x.txt', 'vault_id',"
                " 1, 'source', 'upload'), 'pending')",
                (INGESTION_QUEUE,),
            )
            conn.commit()
            lease = JobLease(
                conn,
                reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
                max_attempts=max_attempts,
            )
            claimed = lease.claim(INGESTION_QUEUE, "o02f-worker")
            conn.commit()
        finally:
            conn.close()
        self.assertIsNotNone(claimed, "expected the seeded row to be claimed")

        pool = SQLiteConnectionPool(db_path, max_size=2)
        try:
            with patch.object(settings, "jobs_max_attempts", max_attempts), \
                    patch.object(
                        settings, "wiki_kms_job_lease_enabled", True
                    ):
                bp = BackgroundProcessor(pool=pool, retry_delay=0.01)

                async def _raising(_task):
                    raise RuntimeError(
                        f"ingest boom {INGEST_SENTINEL}"
                    )

                bp._run_task_processing = _raising
                await bp._process_ingest_job_row(claimed, "o02f-worker")
        finally:
            pool.close_all()

        conn = _conn(db_path)
        try:
            row = conn.execute(
                "SELECT status, error FROM jobs WHERE id = ?",
                (claimed["id"],),
            ).fetchone()
        finally:
            conn.close()
        return dict(row)

    async def test_ingest_requeue_error_omits_sentinel(self):
        # check: AC10 (requeue arm) — RED at base, GREEN after the fix.
        row = await self._drive_ingest_failure(max_attempts=3)
        self.assertEqual(row["status"], "pending")
        error = row["error"] or ""
        self.assertNotIn(
            INGEST_SENTINEL,
            error,
            "the requeue settle must persist a redacted stable code, never"
            " str(exc) (#957)",
        )
        self.assertTrue(
            error.strip(),
            "the requeued row must still carry a non-empty redacted error",
        )

    async def test_ingest_attempt_cap_error_omits_sentinel(self):
        # check: AC10 (attempt-cap arm) — RED at base, GREEN after the fix.
        row = await self._drive_ingest_failure(max_attempts=1)
        self.assertEqual(row["status"], "failed")
        error = row["error"] or ""
        self.assertNotIn(
            INGEST_SENTINEL,
            error,
            "the attempt-cap settle must persist a redacted stable code"
            " (attempt_cap_exceeded is allowed; the raw text is not) (#957)",
        )
        # PRR-019: the cap CODE is load-bearing behaviour — pin it, not just
        # the absence of the raw text.
        self.assertTrue(
            error.startswith("attempt_cap_exceeded:"),
            f"the attempt-cap settle must carry the attempt_cap_exceeded"
            f" code, got {error!r}",
        )
        self.assertTrue(
            error.strip(),
            "the terminally failed row must still carry a non-empty redacted"
            " error",
        )


class TestLegacyReindexErrorRedaction(unittest.IsolatedAsyncioTestCase):
    """AC11 (#957): legacy reindex failure arms persist redacted values."""

    def setUp(self):
        self.tmp_dirs = []

    def _db(self, prefix: str) -> str:
        db_path = _mkdb(prefix)
        self.tmp_dirs.append(os.path.dirname(db_path))
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        return db_path

    async def _drive_reindex_failure(self, prefix: str, embed_all):
        from app.models.database import SQLiteConnectionPool
        from app.services.background_tasks import BackgroundProcessor

        db_path = self._db(prefix)
        conn = _conn(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO document_reindex_jobs (vault_id, trigger_type,"
                " status) VALUES (1, 'manual', 'pending')"
            )
            job_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        pool = SQLiteConnectionPool(db_path, max_size=2)
        try:
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            bp._reindex_embed_all = embed_all
            await bp._process_reindex_job(job_id)
        finally:
            pool.close_all()

        conn = _conn(db_path)
        try:
            row = conn.execute(
                "SELECT status, error FROM document_reindex_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row)

    async def test_reindex_exception_arm_omits_sentinel(self):
        # check: AC11 (catch-all arm) — RED at base, GREEN after the fix.
        async def _raising(_job_id, _vault_id, _retry=None):
            raise RuntimeError(
                f"reindex boom {REINDEX_EXC_SENTINEL}"
            )

        row = await self._drive_reindex_failure(
            "o02f-reindex-exc-", _raising
        )
        self.assertEqual(row["status"], "failed")
        self.assertNotIn(
            REINDEX_EXC_SENTINEL,
            row["error"] or "",
            "the legacy reindex catch-all arm must persist a redacted value,"
            " never str(exc) (#957)",
        )

    async def test_reindex_returned_error_arm_omits_sentinel(self):
        # check: AC11 (returned-error arm) — RED at base, GREEN after the
        # fix. CHECK_WRONG-amended (plan-critic Round 1 approved): the
        # production contract (background_tasks.py _reindex_embed_all
        # docstring + the lease boundary's three-way branch) is that
        # returned STRINGS are trusted operator constants persisted
        # verbatim, while returned exception OBJECTS — the ("failed", {},
        # exc) returns at :4813/:4827 — are the raw-text vector the legacy
        # arm's str(error) stringifies. The fake therefore returns an
        # exception OBJECT.
        async def _failing(_job_id, _vault_id, _retry=None):
            return (
                "failed",
                {},
                RuntimeError(f"identity mismatch {REINDEX_RET_SENTINEL}"),
            )

        row = await self._drive_reindex_failure(
            "o02f-reindex-ret-", _failing
        )
        self.assertEqual(row["status"], "failed")
        self.assertNotIn(
            REINDEX_RET_SENTINEL,
            row["error"] or "",
            "the legacy reindex returned-error arm must persist a redacted"
            " value for returned exception objects, never their raw"
            " str() (#957)",
        )

    async def test_reindex_operator_guidance_persisted_verbatim(self):
        # check: AC11 PRESERVING (carve-out) — GREEN at base, must stay
        # GREEN after the fix: hand-written operator guidance is NOT a raw
        # exception payload and must survive redaction byte-identically.
        from app.services.background_tasks import ReindexOperatorGuidance

        self.assertIn("O02F-GUIDANCE-KEEP", GUIDANCE_TEXT)
        guidance = ReindexOperatorGuidance(GUIDANCE_TEXT)

        async def _raising(_job_id, _vault_id, _retry=None):
            raise guidance

        row = await self._drive_reindex_failure(
            "o02f-reindex-guidance-", _raising
        )
        self.assertEqual(row["status"], "failed")
        self.assertEqual(
            row["error"],
            str(guidance),
            "ReindexOperatorGuidance str() must be persisted verbatim — the"
            " documented carve-out (#957 / #702)",
        )


class TestLeasePathRedactionShape(unittest.TestCase):
    """AC11 PRESERVING (source shape): the lease-path reindex boundary keeps
    the ``redact_ingest_error`` call behind the ``ReindexOperatorGuidance``
    isinstance carve-out — the fix must touch only the unredacted members
    of the family, not this already-correct boundary.

    GREEN at base and must stay GREEN.
    """

    def test_run_reindex_job_row_keeps_guidance_carveout_and_redaction(self):
        with open(BACKGROUND_TASKS, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        func = None
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name == "_run_reindex_job_row":
                func = node
                break
        self.assertIsNotNone(
            func, "_run_reindex_job_row must exist in background_tasks.py"
        )

        # PRR-019: the tokens must appear in a LIVE conditional that guards
        # the persisted error_text — a token-only scan passes even when the
        # carve-out is dead code (e.g. `if False: isinstance(...)`).
        carveout_if = None
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue
            test_names = {
                n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)
            } | {
                n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)
            }
            has_isinstance = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "isinstance"
                for n in ast.walk(node.test)
            )
            if has_isinstance and "ReindexOperatorGuidance" in test_names:
                carveout_if = node
                break
        self.assertIsNotNone(
            carveout_if,
            "_run_reindex_job_row must keep a live isinstance(exc,"
            " ReindexOperatorGuidance) conditional (issue #702/#562"
            " contract) — dead tokens do not count",
        )
        body_text = ast.unparse(carveout_if)
        self.assertIn(
            "redact_ingest_error",
            body_text,
            "the carve-out conditional must be the one routing BaseException"
            " payloads through redact_ingest_error (issue #562 contract)",
        )
        self.assertIn(
            "error_text",
            body_text,
            "the carve-out conditional must assign the persisted error_text",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
