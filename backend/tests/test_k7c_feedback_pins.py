"""K7 feedback-round pins — review findings F-001/F-004/RC12 (#1004).

Round-2 acceptance surface, added after the swarm-pr-feedback round (these
files are NOT frozen by the issue-tracer checkpoint):

1. F-001/RC1 — the NUL bypass: plain ``length(col)`` stops at the first NUL
   byte, so a NUL-prefixed oversized body must be caught by the shipped
   ``length(CAST(col AS BLOB))`` guards on BOTH paths (KMS chat retrieval and
   documents ranked search): fast return, bounded excerpt, evidence still
   present. Under the pre-fix char-only shape these took 334–504 s.
2. F-004/MA-04/RC7 — the KMS list wire contract at the HTTP boundary:
   list responses carry a body bounded to 2048 chars with the true length in
   ``body_chars``; the single-entry detail response carries the full body with
   ``body_chars: null``.
3. RC12 — deep-only-term retrieval on an oversized entry pins the DISCLOSED
   design: the row still surfaces as evidence with a bounded excerpt (which,
   per the disclosed limitation, may not contain the deep term).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from test_kms_routes import KMSFixTestBase  # noqa: E402

from app.api.routes.documents import search_documents  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402
from app.services.kms_retrieval import KMSRetrievalService  # noqa: E402

pytestmark = pytest.mark.usefixtures("ready_vector_store")

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

_DENSE_QUERY = "opmed cdp order"
_BODY_SIZE = 1_200_000
_TIME_BUDGET_S = 5.0
_MAX_EXCERPT_CHARS = 2000
_DEEP_TERM = "zzk7cdeepneedleonlytail"
_LIST_BODY_BOUND = 2048


def build_dense_body(size: int) -> str:
    """~size chars of dense-match text, token-index cycled (K7 convention)."""
    approx_tokens = size // 2 + 64
    parts = []
    for i in range(approx_tokens):
        parts.append(_DENSE_TERMS[i % len(_DENSE_TERMS)])
        parts.append("\n" if i % 8 == 7 else " ")
    return "".join(parts)[:size]


def build_nul_body(size: int) -> str:
    """Oversized body whose FIRST character is NUL (the F-001 bypass shape)."""
    return "\x00" + build_dense_body(size)


async def _allow_policy(user, resource_type, resource_id, action):
    return True


class TestK7CNulGuardKMS(unittest.TestCase):
    """F-001: the shipped blob guard must skip snippet() for a NUL body."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="k7c_nul_kms_")
        self._db = str(Path(self._tmp) / "app.db")
        init_db(self._db)
        run_migrations(self._db)
        self._original_enabled = settings.kms_enabled
        settings.kms_enabled = True
        # F-008 shape (reviewer round-2 finding 4): register the restore as a
        # cleanup so a setUp failure below cannot leak the global.
        self.addCleanup(setattr, settings, "kms_enabled", self._original_enabled)

        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (100, 'K7C')")
            # NUL-prefixed oversized body: length() reports 1 (bypass shape);
            # length(CAST(... AS BLOB)) reports the full byte count (guard hits).
            conn.execute(
                "INSERT INTO kms_entries "
                "(vault_id, slug, title, body, summary, tags_json, source_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    100,
                    "k7c-nul-body",
                    "K7C NUL Body Entry",
                    build_nul_body(_BODY_SIZE),
                    "",
                    "[]",
                    "document",
                    "published",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        settings.kms_enabled = self._original_enabled
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_k7c_nul_prefixed_oversized_body_returns_fast_and_bounded(self):
        from queue import Empty, Queue

        class _Pool:
            def __init__(self, db_path):
                self.db_path = db_path
                self._q = Queue(maxsize=2)

            def get_connection(self):
                try:
                    return self._q.get_nowait()
                except Empty:
                    c = sqlite3.connect(self.db_path, check_same_thread=False)
                    c.row_factory = sqlite3.Row
                    return c

            def release_connection(self, c):
                try:
                    self._q.put_nowait(c)
                except Exception:
                    c.close()

            def close_all(self):
                while True:
                    try:
                        self._q.get_nowait().close()
                    except Empty:
                        return

        pool = _Pool(self._db)
        try:
            start = time.perf_counter()
            results = KMSRetrievalService(pool=pool).retrieve(
                _DENSE_QUERY, vault_id=100
            )
            elapsed = time.perf_counter() - start

            assert results, "evidence for the dense-match query must be returned"
            assert elapsed < _TIME_BUDGET_S, (
                f"NUL-prefixed {_BODY_SIZE}-char body took {elapsed:.2f}s "
                f"(budget {_TIME_BUDGET_S}s) — the byte-length guard must skip "
                "snippet() for this row"
            )
            excerpt = results[0].excerpt
            assert len(excerpt) <= _MAX_EXCERPT_CHARS, (
                f"excerpt is {len(excerpt)} chars (bound {_MAX_EXCERPT_CHARS})"
            )
        finally:
            pool.close_all()


class TestK7CNulGuardDocuments:
    """F-001: the documents content-arm blob guard must skip snippet() too."""

    async def test_k7c_nul_prefixed_parsed_text_returns_fast_and_bounded(self):
        tmp = tempfile.mkdtemp(prefix="k7c_nul_docs_")
        db_path = str(Path(tmp) / "app.db")
        conn = None
        try:
            init_db(db_path)
            run_migrations(db_path)
            seed = sqlite3.connect(db_path)
            try:
                seed.execute("INSERT INTO vaults (id, name) VALUES (100, 'K7C')")
                seed.execute(
                    "INSERT INTO files "
                    "(vault_id, file_path, file_name, file_size, file_type, "
                    " status, parsed_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        100,
                        "/k7c/nul.txt",
                        "k7c-nul.txt",
                        _BODY_SIZE,
                        "txt",
                        "indexed",
                        build_nul_body(_BODY_SIZE),
                    ),
                )
                seed.commit()
            finally:
                seed.close()

            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            start = time.perf_counter()
            response = await search_documents(
                q=_DENSE_QUERY,
                vault_id=100,
                limit=20,
                offset=0,
                conn=conn,
                user={"id": 1, "username": "k7c", "role": "superadmin"},
                evaluate=_allow_policy,
            )
            elapsed = time.perf_counter() - start

            assert response.results, "the dense NUL body must still MATCH"
            assert elapsed < _TIME_BUDGET_S, (
                f"documents search over a NUL-prefixed {_BODY_SIZE}-char body "
                f"took {elapsed:.2f}s (budget {_TIME_BUDGET_S}s)"
            )
            excerpt = response.results[0].excerpt
            assert len(excerpt) <= 300, (
                f"excerpt is {len(excerpt)} chars (bound 300)"
            )
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestK7CDeepTermRetrievalPin(unittest.TestCase):
    """RC12: oversized entry with a term only at the tail still surfaces.

    Pins the DISCLOSED design (review F-003): over-threshold evidence returns
    bounded, and its excerpt MAY not contain the deep term — the behavior the
    fragment discloses, made executable so a future change to either side of
    that contract fails loudly.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="k7c_deep_")
        self._db = str(Path(self._tmp) / "app.db")
        init_db(self._db)
        run_migrations(self._db)
        self._original_enabled = settings.kms_enabled
        settings.kms_enabled = True
        # F-008 shape: cleanup-registered restore (setUp may raise below).
        self.addCleanup(setattr, settings, "kms_enabled", self._original_enabled)

        body = build_dense_body(_BODY_SIZE) + " " + _DEEP_TERM
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (100, 'K7C')")
            conn.execute(
                "INSERT INTO kms_entries "
                "(vault_id, slug, title, body, summary, tags_json, source_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    100,
                    "k7c-deep",
                    "K7C Deep Term Entry",
                    body,
                    "",
                    "[]",
                    "document",
                    "published",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        settings.kms_enabled = self._original_enabled
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_k7c_deep_only_term_returns_bounded_evidence(self):
        from queue import Empty, Queue

        class _Pool:
            def __init__(self, db_path):
                self.db_path = db_path
                self._q = Queue(maxsize=2)

            def get_connection(self):
                try:
                    return self._q.get_nowait()
                except Empty:
                    c = sqlite3.connect(self.db_path, check_same_thread=False)
                    c.row_factory = sqlite3.Row
                    return c

            def release_connection(self, c):
                try:
                    self._q.put_nowait(c)
                except Exception:
                    c.close()

            def close_all(self):
                while True:
                    try:
                        self._q.get_nowait().close()
                    except Empty:
                        return

        pool = _Pool(self._db)
        try:
            results = KMSRetrievalService(pool=pool).retrieve(
                _DEEP_TERM, vault_id=100
            )
            assert results, (
                "a term occurring only beyond the guard threshold must still "
                "surface the entry as evidence (full-body MATCH coverage, AC6)"
            )
            excerpt = results[0].excerpt
            assert len(excerpt) <= _MAX_EXCERPT_CHARS, (
                f"excerpt is {len(excerpt)} chars (bound {_MAX_EXCERPT_CHARS})"
            )
        finally:
            pool.close_all()


class TestK7CWirePin(KMSFixTestBase):
    """F-004/MA-04: the KMS list wire contract, pinned at the HTTP boundary."""

    def test_k7c_list_wire_bounded_with_true_length_detail_full(self):
        # Seed an oversized entry straight into the store's vault, and a small
        # one through the real HTTP create path.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO kms_entries "
                "(vault_id, slug, title, body, summary, tags_json, source_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    2,
                    "k7c-wire-oversized",
                    "K7C Wire Oversized",
                    build_dense_body(_BODY_SIZE),
                    "",
                    "[]",
                    "document",
                    "published",
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        create = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "K7C Wire Small", "body": "world body"},
            headers=self._write_headers(),
        )
        self.assertEqual(create.status_code, 201)
        # MA-02 contract: create/update responses carry body_chars: null
        # alongside a FULL body (key always emitted, null means "full/not-list").
        self.assertIn("body_chars", create.json())
        self.assertIsNone(create.json()["body_chars"])

        resp = self.client.get(
            "/api/kms/entries?vault_id=2", headers=self._write_headers()
        )
        self.assertEqual(resp.status_code, 200)
        entries = {e["title"]: e for e in resp.json()["entries"]}

        oversized = entries["K7C Wire Oversized"]
        self.assertLessEqual(
            len(oversized["body"]),
            _LIST_BODY_BOUND,
            f"list body must be bounded to {_LIST_BODY_BOUND} chars, got "
            f"{len(oversized['body'])}",
        )
        self.assertEqual(
            oversized["body_chars"],
            _BODY_SIZE,
            "list body_chars must report the TRUE stored length",
        )

        # Detail response: full body, body_chars null (documented contract).
        detail_oversized = self.client.get(
            f"/api/kms/entries/{oversized['id']}",
            headers=self._write_headers(),
        )
        self.assertEqual(detail_oversized.status_code, 200)
        full = detail_oversized.json()
        self.assertEqual(
            len(full["body"]), _BODY_SIZE, "detail GET must return the full body"
        )
        self.assertIsNone(
            full.get("body_chars"), "detail body_chars must be null"
        )


if __name__ == "__main__":
    unittest.main()
