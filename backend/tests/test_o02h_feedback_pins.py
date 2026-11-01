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
# PRR-012: the exemption applies to a line that BUILDS an ssrf_blocked
# error string, not to any line that merely mentions it.
_SSRF_SINK_RE = re.compile(r"f.ssrf_blocked:")


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
                # PRR-004: the fan-out is deferred past startup so
                # reconnected SSE clients receive it — wait out the delay
                # (plus margin) instead of a fixed 1s.
                deadline = asyncio.get_event_loop().time() + 12.0
                while (
                    asyncio.get_event_loop().time() < deadline
                    and not any(
                        e.get("job_id") == recent_job.id for e in events
                    )
                ):
                    await asyncio.sleep(0.2)
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
    ``{e!r}``, ``{exc}``, and ``{exc!r}`` variants.

    PRR-012 review fix: the predicate is QUOTE-AGNOSTIC (``f"`` and
    ``f'``) — the previous ``f"`` gate let a single-quoted reintroduction
    through, which the frozen census catches and this one missed — and the
    exemption is by SINK (the ``ssrf_blocked:`` code prefix), not a line
    substring, so a trailing comment cannot exempt a whole line. A positive
    control proves the detector fires.
    """

    _PATTERN = re.compile(r"\{(?:e|exc)(?:!r)?\}")
    _FSTRING = re.compile("f" + chr(34) + "|f" + chr(39))

    def _violations(self, source: str, filename: str) -> dict:
        hits = []
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#") or not self._FSTRING.search(line):
                continue
            if not self._PATTERN.search(line):
                continue
            # Exempt by SINK: the line must actually build an ssrf_blocked
            # error string (a trailing comment mention is not a sink).
            if _SSRF_EXEMPTION in line and _SSRF_SINK_RE.search(line):
                continue
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
            " name only (#958).",
        )

    def test_census_detects_its_own_target_class(self):
        """Positive control: the predicate must fire on known-bad shapes.

        PRR-012: without this, a census that can never fire is
        indistinguishable from a clean tree.
        """
        bad_shapes = [
            'return None, f"transport_error: {type(e).__name__}: {e}"',
            "return None, f'transport_error: {type(e).__name__}: {e}'",
            'raise_error(f"orchestration_error: {exc}")',
            'raise_error(f"persist_error: {exc!r}")',
        ]
        for shape in bad_shapes:
            self.assertEqual(
                self._violations(shape, "synthetic.py"),
                {"synthetic.py": [1]},
                f"the census must flag {shape!r}",
            )

    def test_exemption_is_by_sink_not_by_comment(self):
        """A trailing comment naming ssrf_blocked must not exempt a line."""
        sneaky = (
            'return None, f"transport_error: {type(e).__name__}: {e}"'
            "  # note: unlike ssrf_blocked, this carries the message"
        )
        self.assertEqual(
            self._violations(sneaky, "synthetic.py"),
            {"synthetic.py": [1]},
            "a comment mentioning ssrf_blocked must not exempt a defect line",
        )
        genuine = 'return None, f"ssrf_blocked: {type(e).__name__}"'
        self.assertEqual(
            self._violations(genuine, "synthetic.py"),
            {"synthetic.py": []},
            "the class-only ssrf_blocked sink is the permitted arm",
        )


class TestReadSideMaskContracts(unittest.TestCase):
    """PRR-001/002/005/011/013: the shipped read-side redaction contracts."""

    def test_attempt_cap_prefix_is_remainder_aware(self):
        """PRR-001: the composition prefix must not smuggle raw text."""
        from app.services.job_lease import (
            LEGACY_ERROR_REDACTED,
            redact_legacy_error_text,
        )

        self.assertEqual(
            redact_legacy_error_text(
                "attempt_cap_exceeded: RuntimeError: conn refused"
                " db.internal:5432 pw=hunter2"
            ),
            LEGACY_ERROR_REDACTED,
            "a pre-#792 cap row wrapping raw str(exc) must be masked",
        )
        self.assertEqual(
            redact_legacy_error_text(
                "attempt_cap_exceeded: COMPILE_FAILED: wiki compile dispatch"
                " failed; see server logs"
            ),
            "attempt_cap_exceeded: COMPILE_FAILED: wiki compile dispatch"
            " failed; see server logs",
            "a nested stable code still passes through byte-identically",
        )

    def test_ingest_codes_are_stable_codes(self):
        """PRR-005: the #562 vocabulary is part of the stable-code family."""
        from app.services.job_lease import redact_legacy_error_text

        for code in (
            "PARSE_FAILED: document could not be parsed",
            "FILE_MISSING: the document file is gone",
            "EMBEDDING_FAILED: the embedding service refused the batch",
            "VECTOR_STORE_FAILED: the vector store rejected the write",
        ):
            self.assertEqual(
                redact_legacy_error_text(code),
                code,
                f"{code.split(':')[0]} is a stable code and must pass through",
            )

    def test_non_string_column_values_are_masked_not_raised(self):
        """PRR-011: the read path must not crash on a non-str column value."""
        from app.services.job_lease import (
            LEGACY_ERROR_REDACTED,
            redact_legacy_error_text,
        )

        for value in (b"raw bytes here", 123, 4.5, True):
            self.assertEqual(
                redact_legacy_error_text(value),
                LEGACY_ERROR_REDACTED,
                f"{value!r} must be masked, not raise",
            )
        self.assertIsNone(redact_legacy_error_text(None))
        self.assertEqual(redact_legacy_error_text(""), "")

    def test_marker_value_is_pinned(self):
        """PRR-013: operators see exactly this string; pin it."""
        from app.services.job_lease import (
            LEGACY_ERROR_REDACTED,
            redact_legacy_error_text,
        )

        self.assertEqual(
            LEGACY_ERROR_REDACTED,
            "REDACTED_LEGACY_ERROR: pre-redaction failure detail withheld; "
            "see server logs",
        )
        self.assertEqual(
            redact_legacy_error_text("RuntimeError: raw"),
            LEGACY_ERROR_REDACTED,
        )

    def test_result_json_masks_legacy_error_strings(self):
        """PRR-002: historical curator errors lose their message tail."""
        import json

        from app.services.job_lease import redact_legacy_result_json

        legacy = json.dumps(
            {
                "curator": {
                    "errors": [
                        "transport_error: RuntimeError: Curator returned"
                        " HTTP 401: {\"message\": \"bad key\"}"
                    ],
                    "accepted": 2,
                }
            }
        )
        out = json.loads(redact_legacy_result_json(legacy))
        self.assertEqual(
            out["curator"]["errors"],
            ["transport_error: RuntimeError"],
            "the raw message tail must be dropped",
        )
        self.assertEqual(
            out["curator"]["accepted"],
            2,
            "non-error payload keys must survive",
        )
        clean = json.dumps({"curator": {"errors": ["transport_error: X"]}})
        self.assertEqual(
            redact_legacy_result_json(clean),
            clean,
            "a clean payload must pass through byte-identically",
        )

    def test_result_json_masks_legacy_detail_entries(self):
        """PRR-007: the reindex legacy-persist details list is masked."""
        import json

        from app.services.job_lease import redact_legacy_result_json

        legacy = json.dumps(
            {
                "processed": 3,
                "failed": 1,
                "details": [
                    "file_id=5: PARSE_FAILED: document could not be parsed",
                    "file_id=6: RuntimeError: disk I/O error on /srv/x",
                ],
            }
        )
        out = json.loads(redact_legacy_result_json(legacy))
        self.assertEqual(
            out["details"][0],
            "file_id=5: PARSE_FAILED: document could not be parsed",
            "a composed-clean ingest code must survive byte-identically",
        )
        self.assertNotIn("disk I/O", out["details"][1])
        self.assertTrue(out["details"][1].startswith("file_id=6: "))


class TestMapperWiring(unittest.IsolatedAsyncioTestCase):
    """PRR-002 wiring proof: the mask must be applied AT the mappers, not
    only exist as a helper — reverting either call site must fail here."""

    def setUp(self):
        db_path = _mkdb("o02h-wiring-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        self.db_path = db_path

    async def test_mappers_mask_legacy_result_json(self):
        import json
        from unittest.mock import patch

        from app.models.database import run_migrations  # noqa: F401
        from app.services.kms_store import KMSStore
        from app.services.wiki_store import WikiStore

        legacy_result = json.dumps(
            {
                "curator": {
                    "errors": [
                        "transport_error: RuntimeError: Curator returned HTTP"
                        " 401: {\"message\": \"bad key\"}"
                    ],
                    "accepted": 1,
                }
            }
        )
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with patch.object(settings, "wiki_kms_job_lease_enabled", True):
                wjob = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
                kjob = KMSStore(conn).create_job(1, "manual")
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, "
                    "result_json = ? WHERE id = ?",
                    (
                        "attempt_cap_exceeded: RuntimeError: raw token=sk-live-1",
                        legacy_result,
                        wjob.id,
                    ),
                )
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, "
                    "result_json = ? WHERE id = ?",
                    (
                        "attempt_cap_exceeded: RuntimeError: raw token=sk-live-2",
                        legacy_result,
                        kjob.id,
                    ),
                )
                conn.commit()

                wjobs = WikiStore(conn).list_jobs(1)
                wrow = next(j for j in wjobs if j.id == wjob.id)
                kjobs = KMSStore(conn).list_jobs(1)
                krow = next(j for j in kjobs if j.id == kjob.id)
        finally:
            conn.close()

        for label, row in (("wiki", wrow), ("kms", krow)):
            self.assertNotIn(
                "sk-live",
                row.error or "",
                f"{label}: the cap-prefixed raw error must be masked (PRR-001)",
            )
            curator_errors = json.loads(row.result_json)["curator"]["errors"]
            self.assertEqual(
                curator_errors,
                ["transport_error: RuntimeError"],
                f"{label}: legacy result_json error strings must lose their"
                " message tail at the mapper (PRR-002)",
            )


class TestRealBusDelivery(unittest.IsolatedAsyncioTestCase):
    """PRR-003/PRR-004: the threaded publish arm and the deferred reconcile
    fan-out must deliver through the REAL event bus (not a recording spy),
    and deleting the publish arm must turn these red."""

    def setUp(self):
        db_path = _mkdb("o02h-bus-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        self.db_path = db_path

    async def test_threaded_publish_reaches_a_real_subscriber(self):
        import asyncio
        from unittest.mock import patch

        from app.models.database import SQLiteConnectionPool
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_events import WikiEventBus
        from app.services.wiki_store import WikiStore

        bus = WikiEventBus()
        queue = bus.subscribe(1)
        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        proc = WikiCompileProcessor(pool)
        with pool.connection() as conn:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
            job_id = job.id

        def boom(_job):
            raise RuntimeError("dispatch exploded: O02H-BUS-SENTINEL")

        proc._dispatch = boom
        with patch.object(settings, "wiki_kms_job_lease_enabled", True), \
                patch.object(settings, "jobs_max_attempts", 1), \
                patch(
                    "app.services.wiki_events.get_wiki_event_bus",
                    return_value=bus,
                ):
            await proc.start()
            try:
                deadline = asyncio.get_event_loop().time() + 15.0
                while asyncio.get_event_loop().time() < deadline:
                    conn = sqlite3.connect(self.db_path, timeout=10)
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT status FROM jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    conn.close()
                    if row["status"] in ("failed", "completed", "cancelled"):
                        break
                    await asyncio.sleep(0.1)
                # The threaded unit's publish is marshalled onto the loop;
                # drain the loop so the callback runs.
                for _ in range(20):
                    await asyncio.sleep(0.1)
                    if not queue.empty():
                        break
            finally:
                await proc.stop()
                pool.close_all()

        self.assertFalse(
            queue.empty(),
            "the terminal job_failed event must reach a REAL subscriber"
            " through the threaded publish arm (PRR-003: deleting the"
            " _marshal_publish calls must fail here)",
        )
        delivered = queue.get_nowait()
        self.assertEqual(delivered.get("type"), "job_failed")
        self.assertEqual(delivered.get("job_id"), job_id)

    async def test_reconcile_fanout_is_deferred_and_delivered(self):
        """PRR-004: the reconcile fans out AFTER startup (subscribers exist)."""
        import asyncio
        from datetime import datetime, timedelta
        from unittest.mock import patch

        from app.models.database import SQLiteConnectionPool
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_events import WikiEventBus
        from app.services.wiki_store import WikiStore

        bus = WikiEventBus()
        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        with pool.connection() as conn:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
            job_id = job.id
            conn.execute(
                "UPDATE jobs SET status = 'failed', completed_at = ?, "
                "error = ? WHERE id = ?",
                (
                    datetime.utcnow().isoformat(),
                    "COMPILE_FAILED: wiki compile dispatch failed; see server logs",
                    job_id,
                ),
            )
            conn.commit()

        proc = WikiCompileProcessor(pool)
        with patch.object(settings, "wiki_kms_job_lease_enabled", True), \
                patch(
                    "app.services.wiki_events.get_wiki_event_bus",
                    return_value=bus,
                ):
            await proc.start()
            try:
                # The fan-out is deferred past startup: a subscriber that
                # attaches after start() must still receive the event.
                queue = bus.subscribe(1)
                deadline = asyncio.get_event_loop().time() + 15.0
                while asyncio.get_event_loop().time() < deadline:
                    if not queue.empty():
                        break
                    await asyncio.sleep(0.1)
            finally:
                await proc.stop()
                pool.close_all()

        self.assertFalse(
            queue.empty(),
            "the deferred reconcile fan-out must reach a subscriber that"
            " attached after startup (PRR-004)",
        )
        self.assertEqual(queue.get_nowait().get("job_id"), job_id)



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
