"""K7 acceptance checks — KMSStore list_entries body bounds and search coverage.

Trace: kms-fts-snippet-large-body-wedge. kms_compile_processor copies whole
files.parsed_text into one kms_entries.body cell, and KMSStore.list_entries
SELECT * materializes those full bodies on every list path.

Checks in this file (store-level, no HTTP auth):
- AC5 (DISCRIMINATING): over a vault containing the oversized entry,
  list_entries returns entries whose body is bounded (<= 2048 chars) while
  get_entry on the same entry still returns the full body (>= 1_000_000).
  Both asserts in ONE test.
- AC6 (PRESERVING): full-body FTS search coverage — list_entries(vault_id,
  search=<term that occurs ONLY deep in the oversized body>) still finds the
  entry. This pins that large documents are not truncated out of search.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import init_db, run_migrations  # noqa: E402
from app.services.kms_store import KMSStore  # noqa: E402

# ---------------------------------------------------------------------------
# Dense-body fixture (same shape as the KMS retrieval file)
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

_BODY_SIZE = 1_200_000

# AC5 list-path bound (chars).
_LIST_BODY_BOUND = 2048

# AC5 full-body floor (chars): get_entry must still surface the whole body.
_FULL_BODY_FLOOR = 1_000_000

# AC6 needle: occurs ONLY once, at the very end of the oversized body — never
# in any title, summary, or the normal entry.
_DEEP_NEEDLE = "zzqk7deepneedleunique"


def build_dense_body(size: int) -> str:
    """Return ~size chars of dense-match text, token-index cycled."""
    approx_tokens = size // 2 + 64
    parts = []
    for i in range(approx_tokens):
        parts.append(_DENSE_TERMS[i % len(_DENSE_TERMS)])
        parts.append("\n" if i % 8 == 7 else " ")
    return "".join(parts)[:size]


class _K7StoreBase(unittest.TestCase):
    """Vault 100 with one oversized entry (+ one normal entry for AC5)."""

    #: whether setUp should also insert the small normal companion entry.
    _with_normal_entry = True
    #: whether to append the deep-only needle to the oversized body.
    _with_deep_needle = False

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="k7_kms_store_")
        self._db = str(Path(self._tmp) / "app.db")
        init_db(self._db)
        run_migrations(self._db)
        self._conn = sqlite3.connect(self._db)
        try:
            self._conn.execute(
                "INSERT INTO vaults (id, name) VALUES (100, 'K7V')"
            )
            body = build_dense_body(_BODY_SIZE)
            if self._with_deep_needle:
                body = body + "\n" + _DEEP_NEEDLE + " tail"
            cur = self._conn.execute(
                "INSERT INTO kms_entries "
                "(vault_id, slug, title, body, summary, tags_json, "
                "source_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    100,
                    "k7-oversized-document-entry",
                    "K7 Oversized Document Entry",
                    body,
                    "",
                    "[]",
                    "document",
                    "published",
                ),
            )
            self._oversized_id = cur.lastrowid
            if self._with_normal_entry:
                self._conn.execute(
                    "INSERT INTO kms_entries "
                    "(vault_id, slug, title, body, summary, tags_json, "
                    "source_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        100,
                        "k7-normal-entry",
                        "K7 Normal Entry",
                        "A normal curated note with no oversized content.",
                        "",
                        "[]",
                        "manual",
                        "published",
                    ),
                )
            self._conn.commit()
        except Exception:
            self._conn.close()
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestK7AC5StoreBodyBounds(_K7StoreBase):
    """AC5 (DISCRIMINATING): list_entries bounded bodies, get_entry full body."""

    def test_k7_ac5_list_entries_bounds_body_get_entry_returns_full(self):
        store = KMSStore(self._conn)

        full = store.get_entry(self._oversized_id)
        assert full is not None, "get_entry must find the oversized entry"
        assert len(full.body) >= _FULL_BODY_FLOOR, (
            f"get_entry body shrank to {len(full.body)} chars "
            f"(floor {_FULL_BODY_FLOOR}) — point reads must keep the full body"
        )

        listed = store.list_entries(vault_id=100)
        assert listed, "list_entries must return the vault's entries"
        by_id = {e.id: e for e in listed}
        assert self._oversized_id in by_id, (
            "oversized entry missing from list_entries: "
            f"{sorted(by_id)} vs expected {self._oversized_id}"
        )
        for e in listed:
            assert len(e.body) <= _LIST_BODY_BOUND, (
                f"list_entries returned an unbounded body ({len(e.body)} "
                f"chars, bound {_LIST_BODY_BOUND}) for entry {e.id} "
                f"({e.title!r})"
            )


class TestK7AC6DeepSearchCoverage(_K7StoreBase):
    """AC6 (PRESERVING): deep-in-body term still finds the oversized entry."""

    _with_normal_entry = True
    _with_deep_needle = True

    def test_k7_ac6_search_finds_term_deep_in_oversized_body(self):
        store = KMSStore(self._conn)

        listed = store.list_entries(vault_id=100, search=_DEEP_NEEDLE)
        ids = [e.id for e in listed]
        assert self._oversized_id in ids, (
            f"search for the deep-only term {_DEEP_NEEDLE!r} lost the "
            f"oversized entry; matched ids: {ids}"
        )
