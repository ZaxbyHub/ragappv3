"""Promotion-gate companion pins (issue #1117; complements the frozen
test_o06g_punctuation_promotion_gate.py).

The frozen file pins the internal-punctuation arms (U.S./US, GPU-heavy)
and the preserving controls. These companions add the boundary the plan
critic's round-2/round-3 review required:

- the COMMA collision pair ("model, version 2" vs "model version 2") does
  not promote on the deterministic path (the frozen file does not pin it);
- the edge-punctuation superseded REACTIVATION arm still fires ("Alpha is
  verified" vs "Alpha is verified!") while the internal-punctuation variant
  does not reactivate — with matched-ID guards so a fuzzy-lookup removal
  cannot pass the variant legs vacuously (the superseded row must remain
  superseded AND be the row the variant matched).
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

from app.models.database import run_migrations  # noqa: E402
from app.services.wiki_compiler import WikiCompiler  # noqa: E402
from app.services.wiki_store import WikiStore  # noqa: E402


def _make_env():
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, "
        "file_size, status) VALUES (1, 1, ?, 'test.txt', 100, 'indexed')",
        (str(Path(td) / "test.txt"),),
    )
    conn.commit()
    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)
    return conn, store, compiler, td


class TestCommaPairNoPromotion(unittest.TestCase):
    """The comma pair collides under normalize_claim_text (both normalize
    to "model version 2") — the reproduction classes it in the defect —
    and promotion_identity_key must block the lift (comma is not a
    sentence terminal). GREEN after the fix; RED at base."""

    def test_comma_variant_does_not_promote_unverified(self):
        conn, store, compiler, td = _make_env()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        store.create_claim(
            vault_id=1,
            claim_text="model, version 2 shipped",
            source_type="memory",
            status="unverified",
        )
        claim, created = compiler._find_or_create_claim(
            1,
            "model version 2 shipped",
            "document",
            {"file_id": 1},
            status="active",
            claim_type="fact",
            confidence=0.9,
        )
        self.assertFalse(created)
        after = store.get_claim(claim.id)
        self.assertEqual(
            after.status,
            "unverified",
            "comma-differing variant must not lift an unverified claim",
        )


class TestEdgePunctuationReactivationArms(unittest.TestCase):
    def _seed_superseded(self, store, text):
        claim = store.create_claim(
            vault_id=1,
            claim_text=text,
            source_type="memory",
            status="superseded",
        )
        return claim.id

    def test_edge_bang_variant_still_reactivates_superseded(self):
        conn, store, compiler, td = _make_env()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        cid = self._seed_superseded(store, "Alpha is verified")
        claim, created = compiler._find_or_create_claim(
            1,
            "Alpha is verified!",
            "document",
            {"file_id": 1},
            status="active",
            claim_type="fact",
            confidence=0.9,
        )
        self.assertFalse(created)
        self.assertEqual(claim.id, cid, "matched the seeded claim")
        self.assertEqual(
            store.get_claim(cid).status,
            "active",
            "trailing-'!' variant is sentence-terminal edge punctuation "
            "and must keep reactivating (#929 parity)",
        )

    def test_internal_punctuation_variant_does_not_reactivate(self):
        conn, store, compiler, td = _make_env()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        cid = self._seed_superseded(store, "U.S. economy shrank")
        claim, created = compiler._find_or_create_claim(
            1,
            "US economy shrank",
            "document",
            {"file_id": 1},
            status="active",
            claim_type="fact",
            confidence=0.9,
        )
        # Matched-ID guard: the variant still dedup-matched the seeded row
        # (created=False) — removing the fuzzy lookup entirely would create
        # a NEW claim here and also fail this assert.
        self.assertFalse(created)
        self.assertEqual(claim.id, cid)
        self.assertEqual(
            store.get_claim(cid).status,
            "superseded",
            "internal-punctuation variant must not reactivate",
        )


if __name__ == "__main__":
    unittest.main()
