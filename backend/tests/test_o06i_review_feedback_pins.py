"""swarm-pr-feedback pins for PR #1140 (review run pr1140-20261115).

The review confirmed four mutation-proven test gaps as advisory findings;
these companions close them (frozen files untouched — the pins live here):

- F-002: a DENYING evaluate must not leak an admission slot (the acquire
  sits after the 403 vault-write check; a reorder that acquires first
  leaks one slot per denied request — this pin dies under that reorder).
- F-003: a blocked (non-liftable) punctuation variant must still ATTACH
  its dedup source to the matched claim (gating attach on liftable
  passed every existing test).
- F-004: the curator-arm matched-ID guards (fuzzy-lookup removal leaves
  the numeric gate and superseded-variant arms of the frozen file
  vacuously green — these count-guarded twins die under that removal).
- F-005: interior sentence terminals stay identity-relevant
  (strip-everywhere passed every existing test).
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types_mod = __import__("types").ModuleType(_mod)
        sys.modules[_mod] = types_mod

import app.api.routes.wiki as wiki_route  # noqa: E402
from app.models.database import run_migrations  # noqa: E402
from app.services.wiki_compiler import WikiCompiler  # noqa: E402
from app.services.wiki_linter import WikiLinter  # noqa: E402
from app.services.wiki_store import (  # noqa: E402
    WikiStore,
    promotion_identity_key,
)


class R:
    vault_id = 1


def _slow_run_lint(self, vault_id):
    import time

    time.sleep(0.3)
    return []


async def _deny(user, res, rid, action):
    return False


class TestDeniedEvaluateKeepsCounterAtZero(unittest.TestCase):
    """F-002 pin: the admission acquire must sit AFTER the 403 vault-write
    check — a denied request consumes no slot."""

    def test_403_path_leaves_admission_counter_at_zero(self):
        patcher = patch.object(WikiLinter, "run_lint", _slow_run_lint)
        patcher.start()
        self.addCleanup(patcher.stop)

        def _reset():
            wiki_route._wiki_lint_in_flight = 0

        wiki_route._wiki_lint_in_flight = 0
        self.addCleanup(_reset)

        async def scenario():
            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as ctx:
                await wiki_route.run_wiki_lint(
                    R(), db=sqlite3.connect(":memory:"),
                    user={"id": 1}, evaluate=_deny, _csrf_token="x",
                )
            self.assertEqual(ctx.exception.status_code, 403)

        asyncio.run(scenario())
        self.assertEqual(
            wiki_route._wiki_lint_in_flight, 0,
            "a denied (403) request must not consume an admission slot",
        )


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


class TestBlockedVariantStillAttachesSource(unittest.TestCase):
    """F-003 pin: the fuzzy dedup arm keeps attaching provenance on a
    non-liftable punctuation variant (only the STATUS LIFT is gated)."""

    def test_blocked_variant_attaches_document_source(self):
        conn, store, compiler, td = _make_env()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        store.create_claim(
            vault_id=1,
            claim_text="U.S. economy shrank",
            source_type="memory",
            status="unverified",
        )
        claim, created = compiler._find_or_create_claim(
            1,
            "US economy shrank",
            "document",
            {"file_id": 1},
            status="active",
            claim_type="fact",
            confidence=0.9,
        )
        self.assertFalse(created)
        after = store.get_claim(claim.id)
        self.assertEqual(after.status, "unverified")  # the gate (C2)
        sources = store.get_claim_sources(claim.id)
        self.assertTrue(
            any(getattr(s, "source_kind", None) == "document" for s in sources),
            "the blocked variant must still attach its dedup source row",
        )


class TestCuratorArmMatchedIdGuards(unittest.TestCase):
    """F-004 pins: count-guarded curator arms — these die if the curator
    fuzzy fallback is removed (a NEW claim row would appear)."""

    def _seed_and_lookup(self, existing_text, candidate_text, existing_status):
        conn, store, compiler, td = _make_env()
        store.create_claim(
            vault_id=1,
            claim_text=existing_text,
            source_type="memory",
            status=existing_status,
        )
        claim, created = compiler._find_or_create_claim(
            1,
            candidate_text,
            "document",
            {"file_id": 1},
            status="active",
            claim_type="fact",
            confidence=0.9,
        )
        return conn, store, claim, created, td

    def test_numeric_collision_still_fuzzy_matches_single_row(self):
        # The F-03 numeric gate blocks the LIFT on 5.10-vs-510 — and the
        # lookup must still MATCH (one row, no duplicate created).
        conn, store, claim, created, td = self._seed_and_lookup(
            "The fee is 5.10 USD", "The fee is 510 USD", "unverified"
        )
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        self.assertFalse(created)
        self.assertEqual(claim.status, "unverified")  # numeric gate holds
        count = conn.execute(
            "SELECT COUNT(*) c FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()["c"]
        self.assertEqual(count, 1, "the fuzzy match must not create a row")

    def test_curator_superseded_variant_matches_single_row(self):
        conn, store, claim, created, td = self._seed_and_lookup(
            "U.S. economy shrank", "US economy shrank", "superseded"
        )
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        self.assertFalse(created)
        self.assertEqual(claim.status, "superseded")  # identity gate holds
        count = conn.execute(
            "SELECT COUNT(*) c FROM wiki_claims WHERE vault_id = 1"
        ).fetchone()["c"]
        self.assertEqual(count, 1, "the fuzzy match must not create a row")


class TestInteriorTerminalsStayIdentityRelevant(unittest.TestCase):
    """F-005 pin: interior sentence terminals compare distinct."""

    def test_interior_question_mark_variants_differ(self):
        self.assertNotEqual(
            promotion_identity_key("Is it? true"),
            promotion_identity_key("Is it true"),
        )

    def test_terminal_run_and_edge_still_equivalent(self):
        self.assertEqual(
            promotion_identity_key("Alpha is verified!"),
            promotion_identity_key("Alpha is verified"),
        )
        self.assertEqual(
            promotion_identity_key("wow!!"),
            promotion_identity_key("wow"),
        )


if __name__ == "__main__":
    unittest.main()
