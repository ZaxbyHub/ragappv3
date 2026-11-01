"""Issue #792 (Workstream O, PR 2) acceptance checks: lease-mode claim
survives a malformed jobs payload row.

The unified ``jobs`` table carries per-queue domain fields inside
``payload_json``. Both stores map a claimed row onto their DTO with a bare
``json.loads(payload_json)``, so one row whose payload is not valid JSON (a
corrupted write, a hand-edited row) makes the claim itself raise
``json.JSONDecodeError`` and the worker's poll loop trips over that row on
every pass.

Checks (lease mode on, one planted ``status='pending'`` row with
``payload_json='{not json'``):

- AC5: ``WikiStore.claim_next_pending_job`` completes without raising.
- AC6: ``KMSStore.claim_next_pending_job`` completes without raising.

RED at base (the bare ``json.loads`` in ``_compile_job_from_jobs_row``
raises on the malformed payload, so the claim surfaces JSONDecodeError);
GREEN after the fix (an unparsable payload row is skipped or quarantined by
the claim instead of raising, so the call returns normally).
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from app.config import settings


def _make_db():
    from app.models.database import run_migrations

    db_path = str(Path(mkdtemp()) / "o02-parse.db")
    run_migrations(db_path)  # legacy tables AND the shared jobs table
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    return conn


class TestO02JobsPayloadParse(unittest.TestCase):
    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)

    def _plant_malformed_row(self, conn, queue):
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, 'pending')",
            (queue, "{not json"),
        )
        conn.commit()

    def test_wiki_claim_survives_malformed_payload(self):
        # check: AC5 — the wiki claim call completes without raising when a
        # pending row carries an unparsable payload.
        from app.services.wiki_store import WikiStore

        conn = _make_db()
        self.addCleanup(conn.close)
        self._plant_malformed_row(conn, "wiki")

        err = None
        try:
            WikiStore(conn).claim_next_pending_job("o02-worker")
        except Exception as e:  # noqa: BLE001 — the defect IS the raise itself
            err = type(e).__name__
        assert err is None

    def test_kms_claim_survives_malformed_payload(self):
        # check: AC6 — the KMS claim call completes without raising when a
        # pending row carries an unparsable payload.
        from app.services.kms_store import KMSStore

        conn = _make_db()
        self.addCleanup(conn.close)
        self._plant_malformed_row(conn, "kms")

        err = None
        try:
            KMSStore(conn).claim_next_pending_job("o02-worker")
        except Exception as e:  # noqa: BLE001 — the defect IS the raise itself
            err = type(e).__name__
        assert err is None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
