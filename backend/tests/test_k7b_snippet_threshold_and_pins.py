"""K7 implementation pins — clauses the frozen checks cannot pin (#1002).

The frozen K7 files (test_k7_*) are checkpoint-anchored and cannot be edited;
these non-frozen pins cover the acceptance legs that live below their
resolution (plan-critic round 1 findings 2/3/5/6, round 2 NITs adopted):

1. The snippet-guard threshold stays inside the measured budget envelope
   (0 < FTS_SNIPPET_MAX_COLUMN_BYTES <= 200000). The ceiling measures BYTES —
   the NUL-proof measure (F-001: plain length() stops at the first NUL byte) —
   and the 200000 bound is a budget bound: 8 dense rows just under it cost
   8 x 0.405s ≈ 3.2s, inside the 5s check budget (measured 200K probe), while
   the shipped 131072 sits well inside with realistic-density headroom.
2. list_entries reports the TRUE body length via body_chars while shipping a
   bounded body; the detail read (get_entry) still returns the full body with
   body_chars=None.
3. The over-threshold KMS evidence excerpt is DERIVED FROM THE STORED BODY:
   on an empty-summary fixture it equals the body head window exactly — not
   an arbitrary non-empty string.
4. The documents ranked search over an oversized parsed_text returns a
   bounded, non-empty excerpt.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes.documents import search_documents  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402
from app.services.fts_query import FTS_SNIPPET_MAX_COLUMN_BYTES  # noqa: E402
from app.services.kms_retrieval import KMSRetrievalService  # noqa: E402
from app.services.kms_store import KMSStore  # noqa: E402

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
_LIST_BODY_BOUND = 2048
_EXCERPT_CHARS = 600


def build_dense_body(size: int) -> str:
    """~size chars of dense-match text, token-index cycled (K7 convention)."""
    approx_tokens = size // 2 + 64
    parts = []
    for i in range(approx_tokens):
        parts.append(_DENSE_TERMS[i % len(_DENSE_TERMS)])
        parts.append("\n" if i % 8 == 7 else " ")
    return "".join(parts)[:size]


async def _allow_policy(user, resource_type, resource_id, action):
    return True


def _seed_vault_and_oversized_entry(db_path: str) -> int:
    """Insert vault 100 + one empty-summary oversized dense KMS entry."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO vaults (id, name) VALUES (100, 'K7B')")
        cur = conn.execute(
            "INSERT INTO kms_entries "
            "(vault_id, slug, title, body, summary, tags_json, source_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                100,
                "k7b-oversized",
                "K7B Oversized Entry",
                build_dense_body(_BODY_SIZE),
                "",
                "[]",
                "document",
                "published",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


class TestK7BThresholdPin(unittest.TestCase):
    """Pin 1: the guard threshold cannot drift toward incident scale."""

    def test_threshold_within_budget_envelope(self):
        # Budget rationale (honest measured numbers): snippet() costs ~0.405s
        # on a 200K dense body at probe density, so 8 rows just under the
        # 200000 bound ≈ 3.2s — inside the 5s check budget; at adversarial
        # all-matching density the shipped 131072 measures 1.4–3.3s/row
        # (8-row 11–26s worst case, disclosed). Anything near the ~1M incident
        # scale must never pass this pin.
        assert 0 < FTS_SNIPPET_MAX_COLUMN_BYTES <= 200000, (
            f"FTS_SNIPPET_MAX_COLUMN_BYTES={FTS_SNIPPET_MAX_COLUMN_BYTES} "
            "drifted outside the measured budget envelope (0, 200000]"
        )


class TestK7BListBodyCharsPin(unittest.TestCase):
    """Pin 2: list projection bounds the body and reports its true length."""

    def test_list_bounded_body_and_true_chars(self):
        tmp = tempfile.mkdtemp(prefix="k7b_list_")
        db_path = str(Path(tmp) / "app.db")
        try:
            init_db(db_path)
            run_migrations(db_path)
            entry_id = _seed_vault_and_oversized_entry(db_path)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                store = KMSStore(conn)
                listed = store.list_entries(100, page=1, per_page=10)
                assert len(listed) == 1
                assert len(listed[0].body) <= _LIST_BODY_BOUND, (
                    f"list body is {len(listed[0].body)} chars "
                    f"(bound {_LIST_BODY_BOUND})"
                )
                assert listed[0].body_chars == _BODY_SIZE, (
                    f"body_chars={listed[0].body_chars}, expected "
                    f"{_BODY_SIZE} (the TRUE stored length)"
                )

                detail = store.get_entry(entry_id)
                assert detail is not None
                assert len(detail.body) == _BODY_SIZE, (
                    f"detail body is {len(detail.body)} chars, expected the "
                    "full stored body"
                )
                assert detail.body_chars is None, (
                    "detail read reports body_chars="
                    f"{detail.body_chars}; the full-body detail path should "
                    "leave it unset"
                )
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestK7BExcerptDerivationPin(unittest.TestCase):
    """Pin 3: the oversized-body excerpt is derived from the stored body."""

    def test_oversized_excerpt_equals_body_head_on_empty_summary(self):
        # Empty-summary fixture (plan-critic round-2 NIT): with a non-empty
        # summary the fallback is trivially "equals summary" and proves
        # nothing about body derivation.
        tmp = tempfile.mkdtemp(prefix="k7b_excerpt_")
        db_path = str(Path(tmp) / "app.db")
        pool = None
        original_enabled = settings.kms_enabled
        try:
            init_db(db_path)
            run_migrations(db_path)
            _seed_vault_and_oversized_entry(db_path)

            # The exact head window the retrieval SELECT materializes.
            head = build_dense_body(_BODY_SIZE)[:_EXCERPT_CHARS]

            settings.kms_enabled = True

            from queue import Empty, Queue

            class _Pool:
                def __init__(self, path):
                    self.db_path = path
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

            pool = _Pool(db_path)
            results = KMSRetrievalService(pool=pool).retrieve(
                _DENSE_QUERY, vault_id=100
            )
            assert results, "expected evidence rows for the dense query"
            oversized = [r for r in results if r.source_type == "document"]
            assert oversized, (
                f"oversized entry missing from evidence: "
                f"{[(r.entry_id, r.title) for r in results]}"
            )
            excerpt = oversized[0].excerpt
            assert excerpt == head, (
                "oversized-body excerpt must equal the stored body's head "
                f"window ({_EXCERPT_CHARS} chars); got {len(excerpt)} chars "
                f"starting {excerpt[:60]!r}"
            )
        finally:
            settings.kms_enabled = original_enabled
            if pool is not None:
                pool.close_all()
            shutil.rmtree(tmp, ignore_errors=True)


class TestK7BDocumentsExcerptPin(unittest.TestCase):
    """Pin 4: documents ranked search bounds the oversized-file excerpt."""

    def test_oversized_file_excerpt_bounded_and_non_empty(self):
        tmp = tempfile.mkdtemp(prefix="k7b_docs_")
        db_path = str(Path(tmp) / "app.db")
        conn = None
        try:
            init_db(db_path)
            run_migrations(db_path)
            seed = sqlite3.connect(db_path)
            try:
                seed.execute("INSERT INTO vaults (id, name) VALUES (100, 'K7B')")
                seed.execute(
                    "INSERT INTO files "
                    "(vault_id, file_path, file_name, file_size, file_type, "
                    " status, parsed_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        100,
                        "/k7b/big.txt",
                        "k7b-big.txt",
                        _BODY_SIZE,
                        "txt",
                        "indexed",
                        build_dense_body(_BODY_SIZE),
                    ),
                )
                seed.commit()
            finally:
                seed.close()

            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            response = asyncio.run(
                search_documents(
                    q=_DENSE_QUERY,
                    vault_id=100,
                    limit=20,
                    offset=0,
                    conn=conn,
                    user={"id": 1, "username": "k7b", "role": "superadmin"},
                    evaluate=_allow_policy,
                )
            )
            assert response.results, "expected ranked results for the big file"
            big = [r for r in response.results if r.file_name == "k7b-big.txt"]
            assert big, (
                f"k7b-big.txt missing: {[(r.file_name, r.match_type) for r in response.results]}"
            )
            excerpt = big[0].excerpt
            assert len(excerpt.strip()) > 0, "oversized-file excerpt must not be empty"
            assert len(excerpt) <= 300, (
                f"oversized-file excerpt is {len(excerpt)} chars (bound 300)"
            )
            assert "<mark>" not in excerpt, (
                "oversized file took the snippet path; expected the bounded "
                "head-window fallback (guard should skip snippet())"
            )
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import unittest

    unittest.main()
