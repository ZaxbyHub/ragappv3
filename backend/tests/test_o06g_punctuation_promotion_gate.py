"""Punctuation-identity promotion gate checks (issue #1117 AC2).

Harness mirrors backend/tests/test_o06_curator_claim_parity.py:
``_make_env`` (temp DB + run_migrations + vault/files seed), the
``_FlagSnapshot`` settings guard, and the ``_run_curator_payload``
pattern (stub CuratorClient, ``compile_ingest_job(1, {...})``; chunk id
"1_0").

Frozen spec the implementer must satisfy (NEW-SURFACE, issue #1117 AC2):

- ``normalize_claim_text`` (wiki_store.py) strips ALL punctuation and
  collapses whitespace, so texts that differ by INTERNAL punctuation
  which merges adjacent characters share one normalized key. The pinned
  colliding pair is "U.S. economy shrank" vs "US economy shrank" (both
  normalize to "us economy shrank"). Hyphen-vs-space is NOT a colliding
  pair under the current normalizer (hyphens are dropped, not
  space-replaced) and is deliberately not used.
- BOTH promotion paths -- the deterministic twin
  ``WikiCompiler._find_or_create_claim`` and the inline curator
  find-or-create in ``compile_ingest_job`` -- must gate every status
  lift they perform on a claim found via the LOSSY normalized-text arm
  (``find_claim_by_normalized_text``): a punctuation-variant match may
  not promote unverified -> active and may not reactivate superseded ->
  active. The lookup itself (dedup + source attach) is out of scope and
  must keep finding the variant (issue #1117 scope: only the
  promotion-ordering/fallback arm is in scope).
- Preserved arms: an EXACT-text match still promotes/reactivates (the
  #929 parity), and the curator's F-03 numeric-multiset gate still
  blocks the 5.10-vs-510 collision (issue #1113).

Checks in this file (C2, AC2 of #1117):

- test_punctuation_variant_does_not_promote_unverified (deterministic
  twin): RED at base (base lifts on any found claim), GREEN after the
  fix.
- test_punctuation_variant_does_not_reactivate_superseded
  (deterministic twin): RED at base, GREEN after the fix.
- test_curator_punctuation_variant_does_not_promote: RED at base (the
  numeric-multiset gate compares equal -- no digits on either side --
  so the lift fires), GREEN after the fix.
- test_curator_punctuation_variant_does_not_reactivate_superseded: RED
  at base, GREEN after the fix.
- Exact-match twins (deterministic + curator) and the F-03 numeric arm:
  GREEN at base (pins -- they also guard the variant arms against
  vacuous greens by proving the harness produces active candidates).
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

from app.config import settings  # noqa: E402
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


_FLAG_FIELDS = (
    "wiki_llm_curator_enabled",
    "wiki_llm_curator_run_on_ingest",
    "wiki_llm_curator_url",
    "wiki_llm_curator_model",
    "wiki_llm_curator_mode",
    "wiki_llm_curator_require_quote_match",
    "wiki_llm_curator_require_chunk_id",
    "wiki_llm_curator_run_on_manual",
)


class _FlagSnapshot:
    """Snapshot/restore the curator settings block around a test."""

    def __enter__(self):
        self._snap = {f: getattr(settings, f) for f in _FLAG_FIELDS}
        return self

    def __exit__(self, *exc):
        for f, v in self._snap.items():
            setattr(settings, f, v)
        return False


# The pinned colliding pair: both texts normalize to
# "us economy shrank" (the periods inside "U.S." merge U and S after
# punctuation stripping) while the raw texts are NOT the same words with
# the same punctuation.
SEEDED_TEXT = "U.S. economy shrank"
VARIANT_TEXT = "US economy shrank"


def _seed_claim(store, text, status, source_type="memory"):
    claim = store.create_claim(
        vault_id=1,
        claim_text=text,
        source_type=source_type,
        page_id=None,
        status=status,
        created_by_kind="deterministic",
    )
    return claim.id


def _claim_status(conn, claim_id):
    row = conn.execute(
        "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    return dict(row)["status"] if row else None


def _claim_count(conn):
    row = conn.execute("SELECT COUNT(*) AS n FROM wiki_claims").fetchone()
    return dict(row)["n"]


class TestDeterministicTwinPunctuationGate(unittest.TestCase):
    """C2 (AC2 of issue #1117, NEW-SURFACE) -- the deterministic twin
    ``WikiCompiler._find_or_create_claim``: a punctuation variant found
    via the lossy normalized-text arm must NOT fire the unverified ->
    active promotion nor the superseded -> active reactivation; the
    exact-text arm keeps both lifts (pin)."""

    def setUp(self):
        self.conn, self.store, self.compiler, self._td = _make_env()
        self.addCleanup(shutil.rmtree, self._td, True)

    def tearDown(self):
        self.conn.close()

    def test_punctuation_variant_does_not_promote_unverified(self):
        # check: C2 (AC2 of issue #1117, NEW-SURFACE) -- RED at base,
        # GREEN after the fix.
        cid = _seed_claim(self.store, SEEDED_TEXT, "unverified")
        claim, created = self.compiler._find_or_create_claim(
            1, VARIANT_TEXT, "document", {"file_id": 1},
            status="active", claim_type="fact", confidence=0.9,
        )
        # Guards: the lossy lookup still FOUND the seeded claim (the
        # dedup/attach lookup itself is out of scope and unchanged), so
        # the status assert below reads the claim the lossy arm matched.
        self.assertFalse(created)
        self.assertEqual(claim.id, cid)
        self.assertEqual(_claim_count(self.conn), 1)
        # Discriminating assertion: the status lift must not fire on a
        # punctuation-only variant. At base the claim flips to 'active'.
        self.assertEqual(
            _claim_status(self.conn, cid), "unverified",
            "a claim found only via the lossy normalized-text arm "
            f"({SEEDED_TEXT!r} vs {VARIANT_TEXT!r}) must NOT be promoted "
            "to active -- the texts differ by punctuation, not just "
            "case/whitespace",
        )

    def test_exact_text_still_promotes_unverified(self):
        # Pin (#929 parity must survive the gate): the exact-text arm
        # still lifts unverified -> active. GREEN at base and after the
        # fix. Also guards the variant arm against a vacuous green: this
        # proves the call configuration produces status='active'
        # candidates that DO lift when the texts match exactly.
        cid = _seed_claim(self.store, "Exact twin still promotes", "unverified")
        self.compiler._find_or_create_claim(
            1, "Exact twin still promotes", "document", {"file_id": 1},
            status="active", claim_type="fact", confidence=0.9,
        )
        self.assertEqual(
            _claim_status(self.conn, cid), "active",
            "an EXACT-text match must still promote unverified -> active "
            "(deterministic-path parity preserved)",
        )

    def test_punctuation_variant_does_not_reactivate_superseded(self):
        # check: C2 superseded-reactivation arm (AC2 of issue #1117,
        # NEW-SURFACE) -- RED at base, GREEN after the fix. Same identity
        # requirement as the promotion arm: WIKI-006 reactivation is for
        # a recompile that re-derives the claim text UNCHANGED.
        cid = _seed_claim(self.store, SEEDED_TEXT, "superseded")
        self.compiler._find_or_create_claim(
            1, VARIANT_TEXT, "document", {"file_id": 1},
            status="active", claim_type="fact", confidence=0.9,
        )
        self.assertEqual(
            _claim_status(self.conn, cid), "superseded",
            "a punctuation variant must NOT reactivate a superseded "
            "claim -- only an unchanged-text rederivation may (WIKI-006)",
        )

    def test_exact_text_still_reactivates_superseded(self):
        # Pin: the exact-text arm still reactivates superseded -> active.
        # GREEN at base and after the fix.
        cid = _seed_claim(self.store, "Exact superseded twin", "superseded")
        self.compiler._find_or_create_claim(
            1, "Exact superseded twin", "document", {"file_id": 1},
            status="active", claim_type="fact", confidence=0.9,
        )
        self.assertEqual(
            _claim_status(self.conn, cid), "active",
            "an EXACT-text match must still reactivate superseded -> "
            "active (WIKI-006 preserved)",
        )


class TestCuratorTwinPunctuationGate(unittest.TestCase):
    """C2 (AC2 of issue #1117, NEW-SURFACE) -- the curator twin (inline
    find-or-create in ``compile_ingest_job``): a quote-verified accepted
    claim whose text is a punctuation variant of an existing claim must
    NOT promote it (unverified -> active) nor reactivate it (superseded
    -> active) when matched via the lossy normalized-text arm. The
    exact-match arm and the F-03 numeric gate keep their shipped
    behavior (pins)."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.conn, self.store, self.compiler, self._td = _make_env()
        self.addCleanup(self._flag.__exit__, None, None, None)
        self.addCleanup(shutil.rmtree, self._td, True)

    def tearDown(self):
        self.conn.close()

    def _run_curator_payload(self, payload, chunk_text):
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True

        class _Stub:
            def __init__(self, *a, **k):
                pass

            async def propose(self, *_a, **_k):
                return payload

        with patch("app.services.wiki_compiler.CuratorClient", _Stub):
            return self.compiler.compile_ingest_job(
                1, {"file_id": 1, "text": chunk_text}
            )

    def test_curator_punctuation_variant_does_not_promote(self):
        # check: C2 (AC2 of issue #1117, NEW-SURFACE) -- RED at base
        # (the numeric-multiset gate compares equal -- no digit tokens on
        # either side -- so the lossy-arm lift fires), GREEN after the
        # fix.
        cid = _seed_claim(self.store, SEEDED_TEXT, "unverified", "document")
        payload = json.dumps({"claims": [{
            # Punctuation variant of the seeded claim: the exact-text
            # lookup misses, the normalized lookup hits.
            "claim_text": VARIANT_TEXT,
            "claim_type": "fact",
            # The quote is the SEEDED punctuation form, verbatim in the
            # chunk below, so the candidate is quote-verified active.
            "source_quote": SEEDED_TEXT,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(
            payload, f"{SEEDED_TEXT}. Filler text."
        )
        self.assertEqual(result["curator"]["accepted"], 1)
        # Guard: the curator matched the seeded claim (no fresh duplicate
        # row), so the status assert reads the claim the lossy arm found.
        self.assertEqual(_claim_count(self.conn), 1)
        # Discriminating assertion: no promotion on the variant.
        self.assertEqual(
            _claim_status(self.conn, cid), "unverified",
            "a quote-verified candidate whose text is a punctuation "
            f"variant ({VARIANT_TEXT!r} vs {SEEDED_TEXT!r}) must NOT "
            "promote the matched unverified claim on the curator path",
        )

    def test_curator_exact_text_still_promotes(self):
        # Pin (#929 parity must survive the gate): a quote-verified
        # exact-text match still promotes. GREEN at base and after the
        # fix. Also guards the variant arm against a vacuous green: this
        # proves the harness configuration produces quote-verified
        # ACTIVE candidates that lift when the texts match exactly.
        cid = _seed_claim(
            self.store, "Exact curator twin text", "unverified", "document"
        )
        payload = json.dumps({"claims": [{
            "claim_text": "Exact curator twin text",
            "claim_type": "fact",
            "source_quote": "Exact curator twin text",
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(
            payload, "Exact curator twin text. Filler text."
        )
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(
            _claim_status(self.conn, cid), "active",
            "an EXACT-text quote-verified candidate must still promote "
            "the matched unverified claim (curator-path parity preserved)",
        )

    def test_curator_numeric_gate_still_blocks_number_collision(self):
        # Pin (F-03, issue #1113): the numeric-multiset gate still blocks
        # the 5.10-vs-510 lossy collision. GREEN at base and after the
        # fix -- the punctuation gate must not weaken the numeric gate.
        cid = _seed_claim(
            self.store, "The fee is 510 USD", "unverified", "document"
        )
        payload = json.dumps({"claims": [{
            # Normalizes equal to the seeded "The fee is 510 USD"
            # (punctuation stripped) but carries different numbers.
            "claim_text": "The fee is 5.10 USD",
            "claim_type": "fact",
            "source_quote": "The fee is 5.10 USD",
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(
            payload, "The fee is 5.10 USD. Filler text."
        )
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(
            _claim_status(self.conn, cid), "unverified",
            "the F-03 numeric identity gate must keep blocking the "
            "5.10-vs-510 promotion on the lossy-match path",
        )

    def test_curator_punctuation_variant_does_not_reactivate_superseded(self):
        # check: C2 superseded-reactivation arm (AC2 of issue #1117,
        # NEW-SURFACE) -- RED at base (the curator's inline reactivation
        # has no identity gate on the lossy arm), GREEN after the fix.
        cid = _seed_claim(self.store, SEEDED_TEXT, "superseded", "document")
        payload = json.dumps({"claims": [{
            "claim_text": VARIANT_TEXT,
            "claim_type": "fact",
            "source_quote": SEEDED_TEXT,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(
            payload, f"{SEEDED_TEXT}. Filler text."
        )
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(
            _claim_status(self.conn, cid), "superseded",
            "a punctuation variant must NOT reactivate a superseded "
            "claim on the curator path -- only an unchanged-text "
            "rederivation may",
        )

    def test_curator_exact_text_still_reactivates_superseded(self):
        # Pin: the exact-text arm still reactivates superseded -> active
        # on the curator path. GREEN at base and after the fix.
        cid = _seed_claim(
            self.store, "Exact curator superseded twin", "superseded",
            "document",
        )
        payload = json.dumps({"claims": [{
            "claim_text": "Exact curator superseded twin",
            "claim_type": "fact",
            "source_quote": "Exact curator superseded twin",
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(
            payload, "Exact curator superseded twin. Filler text."
        )
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(
            _claim_status(self.conn, cid), "active",
            "an EXACT-text quote-verified candidate must still reactivate "
            "a superseded claim (curator-path parity preserved)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
