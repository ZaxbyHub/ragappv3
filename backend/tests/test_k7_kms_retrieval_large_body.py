"""K7 acceptance checks — KMSRetrievalService over oversized dense bodies.

Trace: kms-fts-snippet-large-body-wedge (production incident: chat queries
wedged worker threads; root cause is FTS5 snippet() evaluated over the
unbounded kms_entries.body column in KMSRetrievalService._retrieve_sync).

Checks in this file:
- AC1 (DISCRIMINATING): retrieve() over a vault holding one ~1.2M-char
  dense-match KMS entry completes within a 5s budget and returns non-empty
  evidence. At base this is RED (measured locally: >2x the budget).
- AC3 (PRESERVING): the same oversized entry still comes back as evidence
  with a bounded excerpt (<= 2000 chars) and no exception. At base this is
  GREEN (slow) — the timing discrimination is AC1's job.

The dense body builder cycles the term list by TOKEN INDEX (not char-count
modulo): slicing the joined text to a char budget can silently drop the
trailing AND-terms and turn the MATCH into 0 rows; index cycling plus a
generous token pool keeps every term dense at every prefix of the body.

Authored by the issue-tracer acceptance-check pass. This file is the frozen
spec for the fix; it must not import or monkeypatch app internals beyond
constructing the service against a real tempdir DB.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402
from app.services.kms_retrieval import KMSRetrievalService  # noqa: E402

# ---------------------------------------------------------------------------
# Dense-body fixture (mirrors the isolated probe from the trace)
# ---------------------------------------------------------------------------

_DENSE_TERMS = [
    "opmed",
    "cdp",
    "order",
    "medications",
    "permission",
    "health",
    "system",
    "t6",
    "row",
    "value",
    "status",
    "active",
]

# Query whose prefix-matched tokens all hit the dense stream densely.
_DENSE_QUERY = "opmed cdp order"

# Oversized body target. Base measurement on this machine (see checks-draft):
# snippet() over a dense body of this size takes well over 2x the budget.
_BODY_SIZE = 1_200_000

# Timing ceiling for AC1 (seconds).
_TIME_BUDGET_S = 5.0

# AC3 excerpt bound (chars). Post-snippet excerpts are ~24 tokens; anything
# up to this bound is acceptable evidence, a full-body dump is not.
_MAX_EXCERPT_CHARS = 2000


def build_dense_body(size: int) -> str:
    """Return ~size chars of dense-match text, token-index cycled.

    Every term appears every 12 tokens across the WHOLE string, so FTS5
    prefix queries over any subset of the terms match immediately at any
    position, and match density stays ~1 hit per 12 chars as in the probe.
    """
    approx_tokens = size // 2 + 64  # generous pool; sliced down below.
    parts = []
    for i in range(approx_tokens):
        parts.append(_DENSE_TERMS[i % len(_DENSE_TERMS)])
        parts.append("\n" if i % 8 == 7 else " ")
    return "".join(parts)[:size]


class _Pool:
    """Minimal connection pool exposing get_connection/release_connection."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._q = Queue(maxsize=5)

    def get_connection(self):
        try:
            return self._q.get_nowait()
        except Empty:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

    def release_connection(self, conn):
        try:
            self._q.put_nowait(conn)
        except Exception:
            conn.close()

    def close_all(self):
        while True:
            try:
                self._q.get_nowait().close()
            except Empty:
                break


class _K7KmsRetrievalBase(unittest.TestCase):
    """Vault 100 with one oversized dense entry + one normal entry."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="k7_kms_retrieval_")
        self._db = str(Path(self._tmp) / "app.db")
        init_db(self._db)
        run_migrations(self._db)
        self._pool = _Pool(self._db)
        self.service = KMSRetrievalService(pool=self._pool)
        self._kms_enabled_original = settings.kms_enabled
        settings.kms_enabled = True
        # Review F-008/P-4 (RC14): unittest does NOT run tearDown when setUp
        # raises, and the seeding block below can raise — register the restore
        # as a cleanup immediately so a failed setup cannot leak the global
        # into later tests in the same xdist worker (repo convention:
        # addCleanup, e.g. test_change_password.py). tearDown's restore stays
        # as a harmless second idempotent write.
        self.addCleanup(setattr, settings, "kms_enabled", self._kms_enabled_original)

        self._oversized_body = build_dense_body(_BODY_SIZE)
        conn = sqlite3.connect(self._db)
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (100, 'K7V')")
            cur = conn.execute(
                "INSERT INTO kms_entries "
                "(vault_id, slug, title, body, summary, tags_json, source_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    100,
                    "k7-oversized-document-entry",
                    "K7 Oversized Document Entry",
                    self._oversized_body,
                    "",
                    "[]",
                    "document",
                    "published",
                ),
            )
            self._oversized_id = cur.lastrowid
            conn.execute(
                "INSERT INTO kms_entries "
                "(vault_id, slug, title, body, summary, tags_json, source_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    100,
                    "k7-normal-entry",
                    "K7 Normal Entry",
                    "A normal curated note about opmed ordering practice.",
                    "",
                    "[]",
                    "manual",
                    "published",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        settings.kms_enabled = self._kms_enabled_original
        self._pool.close_all()
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestK7AC1KmsRetrievalTiming(_K7KmsRetrievalBase):
    """AC1 (DISCRIMINATING): bounded retrieve() latency over a dense oversized body."""

    def test_k7_ac1_retrieve_dense_oversized_body_within_budget(self):
        start = time.perf_counter()
        results = self.service.retrieve(_DENSE_QUERY, vault_id=100)
        elapsed = time.perf_counter() - start

        assert results, "expected non-empty evidence for the dense-match query"
        assert elapsed < _TIME_BUDGET_S, (
            f"KMSRetrievalService.retrieve over a {_BODY_SIZE}-char dense body "
            f"took {elapsed:.2f}s (budget {_TIME_BUDGET_S}s)"
        )


class TestK7AC3OversizedEvidence(_K7KmsRetrievalBase):
    """AC3 (PRESERVING): oversized entry still surfaces as bounded-excerpt evidence."""

    def test_k7_ac3_oversized_entry_returns_bounded_excerpt(self):
        results = self.service.retrieve(_DENSE_QUERY, vault_id=100)

        assert results, "expected evidence rows for the dense-match query"
        oversized = [r for r in results if r.entry_id == self._oversized_id]
        assert oversized, (
            f"oversized entry id {self._oversized_id} missing from evidence: "
            f"{[(r.entry_id, r.title) for r in results]}"
        )

        excerpt = oversized[0].excerpt
        assert len(excerpt) <= _MAX_EXCERPT_CHARS, (
            f"oversized-entry excerpt is {len(excerpt)} chars "
            f"(bound {_MAX_EXCERPT_CHARS})"
        )
        assert len(excerpt.strip()) > 0, "excerpt must not be empty"


if __name__ == "__main__":
    unittest.main()
