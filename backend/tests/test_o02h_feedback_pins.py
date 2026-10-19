"""Plan-critic-mandated hardening pins for the collated jobs-error-hygiene
fix (issues #953/#954/#955/#957/#958).

Non-frozen additions alongside the frozen o02c-o02g checks (additive
hardening lives in its own file so no frozen blob changes):

- the legacy reindex ``json.dumps(result)`` arm: ``result["details"]``
  entries are redacted AT THE PERSIST BOUNDARY (fake-driven — valid
  because the boundary mapping is unconditional regardless of composer);
- ``ReindexOperatorGuidance`` arriving as a RETURNED exception object
  (the production shape: raised inside ``_reindex_embed_all``, caught,
  returned) persists verbatim through the returned-error arm;
- the wiki/KMS row mappers pass the janitor's ``lease_attempt_cap_exceeded``
  code through byte-identically (operator-readable, not masked);
- a widened raw-interpolation census over the curator/compiler
  error-string surface: any raw ``{e}``/``{e!r}``/``{exc}``/``{exc!r}``
  interpolation in an error-sink string construction is a violation, with
  exactly ONE named exemption — the by-design ``ssrf_blocked: {e}`` arm
  (wiki_curator.py). Prefix-based exemption would be vacuous (every defect
  sink carries a stable-code prefix), so the exemption is by name.
"""

import json
import os
import re
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN", "o02h-token-0123456789abcdef0123456789abcdef"
)

from app.config import settings  # noqa: E402

DETAILS_SENTINEL = "O02H-SENTINEL-DETAILS-RAW pw=hunter2"
GUIDANCE_TEXT = (
    "O02H-GUIDANCE-RETURNED-KEEP: model identity changed mid-reindex; "
    "run a full reindex to rebuild the index"
)

SERVICES_DIR = Path(__file__).resolve().parent.parent / "app" / "services"
CURATOR_SOURCE = (SERVICES_DIR / "wiki_curator.py").read_text(encoding="utf-8")
COMPILER_SOURCE = (SERVICES_DIR / "wiki_compiler.py").read_text(encoding="utf-8")

# The ssrf_blocked arm is the single named exemption (short blocker
# detail, by design — see the #958 release fragment).
_SSRF_EXEMPTION = "ssrf_blocked"


def _mkdb(prefix: str) -> str:
    from app.models.database import run_migrations

    tmp_dir = mkdtemp(prefix=prefix)
    db_path = str(Path(tmp_dir) / "o02h.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    conn.close()
    return db_path


class TestLegacyReindexDetailsBoundary(unittest.IsolatedAsyncioTestCase):
    """The json.dumps(result) arm redacts details at the persist boundary."""

    def setUp(self):
        db_path = _mkdb("o02h-details-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        self.db_path = db_path

    async def test_details_arm_redacts_at_persist_boundary(self):
        from app.models.database import SQLiteConnectionPool
        from app.services.background_tasks import BackgroundProcessor

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "INSERT INTO document_reindex_jobs (vault_id, trigger_type,"
                " status) VALUES (1, 'manual', 'pending')"
            )
            job_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        async def _failing(_job_id, _vault_id, _retry=None):
            return (
                "failed",
                {
                    "processed": 2,
                    "failed": 1,
                    "details": [f"file_id=1: boom {DETAILS_SENTINEL}"],
                },
                RuntimeError("aggregate failure"),
            )

        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        try:
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            bp._reindex_embed_all = _failing
            await bp._process_reindex_job(job_id)
        finally:
            pool.close_all()

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, error FROM document_reindex_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "failed")
        self.assertNotIn(
            DETAILS_SENTINEL,
            row["error"] or "",
            "the legacy json.dumps(result) persist must redact raw detail"
            " entries at the boundary regardless of composer (#957)",
        )
        self.assertIn(
            "file_id=1:",
            row["error"] or "",
            "the detail entry's file_id prefix must survive the mapping",
        )

    async def test_guidance_returned_as_object_persists_verbatim(self):
        """Production shape: guidance is RAISED inside _reindex_embed_all,
        CAUGHT there, and RETURNED as the error object — the returned arm's
        carve-out is load-bearing (a naive redact_ingest_error would
        flatten it to a vector-store code)."""
        from app.models.database import SQLiteConnectionPool
        from app.services.background_tasks import (
            BackgroundProcessor,
            ReindexOperatorGuidance,
        )

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "INSERT INTO document_reindex_jobs (vault_id, trigger_type,"
                " status) VALUES (1, 'manual', 'pending')"
            )
            job_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        guidance = ReindexOperatorGuidance(GUIDANCE_TEXT)

        async def _failing(_job_id, _vault_id, _retry=None):
            return ("failed", {}, guidance)

        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        try:
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            bp._reindex_embed_all = _failing
            await bp._process_reindex_job(job_id)
        finally:
            pool.close_all()

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, error FROM document_reindex_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(
            row["error"],
            str(guidance),
            "ReindexOperatorGuidance returned as an exception object must"
            " persist verbatim through the returned-error arm (#957/#702)",
        )

    async def test_details_arm_passes_composed_ingest_codes_through(self):
        """Review-round pin: a detail entry the composition layer already
        redacted (``file_id=N: <#562 code>: reason``) must survive the
        persist boundary BYTE-IDENTICALLY — the boundary's trusted set is
        the full closed #562 vocabulary the composer emits (derived from
        ``_INGEST_ERROR_REASONS``, so every code — including
        ``PARSE_TIMEOUT`` from ``ParseDeadlineError.ingest_error_code`` —
        is covered and future codes extend the pin automatically), not
        just the wiki/KMS compile vocabulary in
        ``redact_legacy_error_text``."""
        from app.models.database import SQLiteConnectionPool
        from app.services.background_tasks import BackgroundProcessor
        from app.services.document_processor import (
            _INGEST_ERROR_REASONS,
            format_ingest_error,
        )

        composed = [
            f"file_id={i}: {format_ingest_error(code)}"
            for i, code in enumerate(_INGEST_ERROR_REASONS, start=5)
        ]
        composed.append(f"file_id=1: boom {DETAILS_SENTINEL}")

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "INSERT INTO document_reindex_jobs (vault_id, trigger_type,"
                " status) VALUES (1, 'manual', 'pending')"
            )
            job_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        async def _failing(_job_id, _vault_id, _retry=None):
            return (
                "failed",
                {"processed": 2, "failed": len(composed), "details": list(composed)},
                RuntimeError("aggregate failure"),
            )

        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        try:
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            bp._reindex_embed_all = _failing
            await bp._process_reindex_job(job_id)
        finally:
            pool.close_all()

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, error FROM document_reindex_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "failed")
        persisted = json.loads(row["error"])["details"]
        for entry in composed[:-1]:
            self.assertIn(
                entry,
                persisted,
                f"composed-clean detail entry must pass the boundary"
                f" byte-identically: {entry!r}",
            )
        self.assertNotIn(
            DETAILS_SENTINEL,
            row["error"] or "",
            "the raw sentinel entry must still be masked at the boundary",
        )
        self.assertTrue(
            any(e.startswith("file_id=1: REDACTED_LEGACY_ERROR") for e in persisted),
            "the raw entry maps to the marker form with its prefix kept",
        )


class TestJanitorCodePassThrough(unittest.TestCase):
    """The mappers keep the janitor's cap-settle code operator-readable."""

    def test_lease_attempt_cap_exceeded_passes_through(self):
        from unittest.mock import patch

        from app.services.kms_store import KMSStore
        from app.services.wiki_store import WikiStore

        db_path = _mkdb("o02h-janitor-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        code = "lease_attempt_cap_exceeded"
        with patch.object(settings, "wiki_kms_job_lease_enabled", True):
            conn = sqlite3.connect(db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                wjob = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (code, wjob.id),
                )
                conn.commit()
                wjobs = WikiStore(conn).list_jobs(1)
                wrow = next(j for j in wjobs if j.id == wjob.id)
                self.assertEqual(
                    wrow.error,
                    code,
                    "the janitor's cap-settle code must pass through the"
                    " wiki mapper byte-identically (#953)",
                )

                kjob = KMSStore(conn).create_job(1, "manual")
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (code, kjob.id),
                )
                conn.commit()
                kjobs = KMSStore(conn).list_jobs(1)
                krow = next(j for j in kjobs if j.id == kjob.id)
                self.assertEqual(
                    krow.error,
                    code,
                    "the janitor's cap-settle code must pass through the"
                    " kms mapper byte-identically (#953)",
                )
            finally:
                conn.close()


class TestReconcileWindowIsoformat(unittest.IsolatedAsyncioTestCase):
    """Review finding 1 pin: the boot-reconcile window compares time-of-day
    for BOTH stored timestamp shapes (SQLite CURRENT_TIMESTAMP and Python
    isoformat 'T'-separated), so an old isoformat row is NOT re-announced
    and a recent one IS — the window is bounded for every writer."""

    def setUp(self):
        db_path = _mkdb("o02h-window-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        self.db_path = db_path

    async def test_isoformat_window_is_time_bounded(self):
        import asyncio
        from datetime import datetime, timedelta
        from unittest.mock import patch

        from app.models.database import SQLiteConnectionPool
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with patch.object(settings, "wiki_kms_job_lease_enabled", True):
                old_job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
                recent_job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
                # The two writer shapes that are NOT SQLite
                # CURRENT_TIMESTAMP: Python isoformat with 'T' separator.
                old_ts = (datetime.utcnow() - timedelta(hours=2)).isoformat()
                recent_ts = (datetime.utcnow() - timedelta(seconds=3)).isoformat()
                conn.execute(
                    "UPDATE jobs SET status = 'failed', completed_at = ?, "
                    "error = ? WHERE id = ?",
                    (
                        old_ts,
                        "COMPILE_FAILED: wiki compile dispatch failed; see server logs",
                        old_job.id,
                    ),
                )
                conn.execute(
                    "UPDATE jobs SET status = 'failed', completed_at = ?, "
                    "error = ? WHERE id = ?",
                    (
                        recent_ts,
                        "COMPILE_FAILED: wiki compile dispatch failed; see server logs",
                        recent_job.id,
                    ),
                )
                conn.commit()
        finally:
            conn.close()

        events: list = []

        class _Bus:
            def publish(self, vault_id, payload):
                events.append({"vault_id": vault_id, **payload})

        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        proc = WikiCompileProcessor(pool)
        try:
            with patch.object(
                settings, "wiki_kms_job_lease_enabled", True
            ), patch(
                "app.services.wiki_events.get_wiki_event_bus", return_value=_Bus()
            ):
                await proc.start()
                await asyncio.sleep(1.0)
        finally:
            await proc.stop()
            pool.close_all()

        announced_ids = {e.get("job_id") for e in events}
        self.assertNotIn(
            old_job.id,
            announced_ids,
            "an isoformat-written terminal row 2h old must NOT be re-announced"
            " by the boot reconcile (the window is time-of-day bounded, not"
            " byte-order — review finding 1)",
        )
        self.assertIn(
            recent_job.id,
            announced_ids,
            "an isoformat-written terminal row 3s old MUST be re-announced by"
            " the boot reconcile (format-agnostic comparison)",
        )


class TestWidenedRawInterpolationCensus(unittest.TestCase):
    """No raw exception-message interpolation on the result_json surface.

    Wider than the frozen C15 census (which pins the exact
    ``{type(e).__name__}: {e}`` shape): this predicate catches ``{e}``,
    ``{e!r}``, ``{exc}``, and ``{exc!r}`` variants, with exactly one named
    exemption (the by-design ``ssrf_blocked:`` arm). RED at base (three
    defect sites), GREEN after the fix.
    """

    _PATTERN = re.compile(r"\{(?:e|exc)(?:!r)?\}")

    def _violations(self, source: str, filename: str) -> dict:
        hits = []
        for lineno, line in enumerate(source.splitlines(), start=1):
            # Both f-string quote styles gate the scan: a future regression
            # written as f'...{e}...' must not escape the census (Copilot
            # review round on the widened predicate).
            if not line.lstrip().startswith("#") and (
                'f"' in line or "f'" in line
            ):
                if self._PATTERN.search(line) and _SSRF_EXEMPTION not in line:
                    hits.append(lineno)
        return {filename: hits}

    def test_no_raw_exception_message_interpolation(self):
        violations = {}
        violations.update(self._violations(CURATOR_SOURCE, "wiki_curator.py"))
        violations.update(self._violations(COMPILER_SOURCE, "wiki_compiler.py"))
        self.assertEqual(
            violations,
            {"wiki_curator.py": [], "wiki_compiler.py": []},
            "raw exception-message interpolation in error strings reaches"
            " vault readers through result_json; use the exception class"
            " name only (#958). The only permitted raw arm is the"
            " ssrf_blocked: short blocker detail.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
