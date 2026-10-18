"""Issue #794 feedback-round pins (swarm-pr-review pr968-20261017).

Companion to test_o04_wiki_recompile_supersede.py (frozen) and
test_o04b_recompile_supersede_policy.py. Each test names the review finding it
pins (PRR-n from the swarm review, F-n from the independent out-of-band
review) — see the PR #968 closure ledger. RED on the pre-feedback tree where
the behavior is new (lifecycle, chat-echo exclusion, unescape arm,
token-boundary pad, whitespace fail-open, result keys, curator
reactivation); coverage-completion tests are GREEN on both trees by design.
Harness mirrors test_o04b/test_issue515.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto

from app.config import settings  # noqa: E402
from app.models.database import run_migrations  # noqa: E402
from app.services.wiki_compiler import WikiCompiler  # noqa: E402
from app.services.wiki_store import WikiStore  # noqa: E402

V1 = "Alice Smith is the AFOMIS Director. Carol Doe is the AFOMIS Deputy Director."
V2 = "Bob Jones is the AFOMIS Director. Carol Doe is the AFOMIS Deputy Director."
ALICE = "Alice Smith is the AFOMIS Director."


def _make_env(vaults=(1,)):
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    for vid in vaults:
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (?, 'T')", (vid,))
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, file_size, status) "
        "VALUES (1, 1, ?, 'test.txt', 100, 'indexed')",
        (str(Path(td) / "test.txt"),),
    )
    conn.commit()
    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)
    return conn, store, compiler, td


class _O04cBase(unittest.TestCase):
    def setUp(self):
        self.conn, self.store, self.compiler, self._td = _make_env()
        self._snap = getattr(settings, "wiki_llm_curator_enabled")
        settings.wiki_llm_curator_enabled = False

    def tearDown(self):
        settings.wiki_llm_curator_enabled = self._snap
        self.conn.close()
        shutil.rmtree(self._td, ignore_errors=True)

    def _page_id(self):
        row = self.conn.execute(
            "SELECT page_id FROM wiki_page_files WHERE file_id = 1"
        ).fetchone()
        return dict(row)["page_id"]

    def _seed(self, claim_text, *, quote, page_id=None, vault_id=1, status="active",
              created_by_kind=None):
        claim = self.store.create_claim(
            vault_id=vault_id, claim_text=claim_text, source_type="document",
            page_id=page_id, status=status, created_by_kind=created_by_kind,
        )
        self.store.attach_source(
            claim_id=claim.id, source_kind="document", file_id=1,
            chunk_id="1_0", source_label="file:1", quote=quote, confidence=0.8,
        )
        self.conn.commit()
        return claim.id

    def _status(self, claim_id):
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        return dict(row)["status"] if row else None

    def _findings(self, claim_id, finding_type, status="open"):
        return self.conn.execute(
            "SELECT title, severity, origin, related_page_ids_json, details "
            "FROM wiki_lint_findings WHERE finding_type = ? AND status = ? "
            "AND related_claim_ids_json = ?",
            (finding_type, status, f"[{claim_id}]"),
        ).fetchall()


VT1 = "Zoe Quinn is the SIGMA Director. " + V2


class TestLifecycleFlap(_O04cBase):
    """PRR-003 / F-12: reactivation deletes the OPEN stale finding; a later
    genuine stale event after the flap produces a fresh one. Flap vehicle:
    the Zoe sentence leaves and returns via the compiled text."""

    def test_reactivation_deletes_open_finding_and_flap_reinserts(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        zoe = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = 'Zoe Quinn is the SIGMA Director.'"
        ).fetchone()[0]
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(zoe), "superseded")
        self.assertEqual(len(self._findings(zoe, "stale")), 1)
        # Restore: the deterministic pass re-derives Zoe -> reactivated and
        # the open stale finding is deleted.
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        self.assertEqual(self._status(zoe), "active")
        self.assertEqual(len(self._findings(zoe, "stale")), 0)
        self.assertEqual(len(self._findings(zoe, "stale", status="resolved")), 0)
        # Flap again: a genuine new stale event surfaces a FRESH open finding.
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(zoe), "superseded")
        self.assertEqual(len(self._findings(zoe, "stale")), 1)

    def test_reactivation_preserves_user_dispositioned_rows(self):
        """PRR-004 corollary: only OPEN rows are deleted on reactivation; a
        resolved row is evidence of a user decision and stays."""
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        zoe = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = 'Zoe Quinn is the SIGMA Director.'"
        ).fetchone()[0]
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        rows = self._findings(zoe, "stale")
        self.assertEqual(len(rows), 1)
        fid = self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE related_claim_ids_json = ?",
            (f"[{zoe}]",),
        ).fetchone()[0]
        self.store.update_lint_finding(fid, 1, "resolved")
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        self.assertEqual(self._status(zoe), "active")
        self.assertEqual(len(self._findings(zoe, "stale", status="resolved")), 1)
        self.assertEqual(len(self._findings(zoe, "stale")), 0)


class TestDispositionSuppression(_O04cBase):
    """PRR-004: a user-terminal disposition (resolved/dismissed/acknowledged)
    permanently suppresses the finding fingerprint - no resurrection."""

    def _prepare(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        zoe = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = 'Zoe Quinn is the SIGMA Director.'"
        ).fetchone()[0]
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        fid = self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE related_claim_ids_json = ?",
            (f"[{zoe}]",),
        ).fetchone()[0]
        return zoe, fid

    def test_resolved_finding_not_resurrected_by_flap(self):
        zoe, fid = self._prepare()
        self.store.update_lint_finding(fid, 1, "resolved")
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(len(self._findings(zoe, "stale")), 0)
        self.assertEqual(len(self._findings(zoe, "stale", status="resolved")), 1)

    def test_dismissed_finding_not_resurrected_by_flap(self):
        zoe, fid = self._prepare()
        self.store.update_lint_finding(fid, 1, "dismissed")
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(len(self._findings(zoe, "stale")), 0)


class TestChatEchoNotSupport(_O04cBase):
    """F-1: a chat-synthesis usage echo must not keep a stale document claim
    alive; a genuine non-echo manual source still counts as support."""

    def test_chat_synthesis_echo_does_not_prevent_supersede(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        alice = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = ?", (ALICE,)
        ).fetchone()[0]
        # Simulate the query path's uncited-answer reuse: a manual
        # chat_synthesis source attached to the ingest claim.
        self.store.attach_source(
            claim_id=alice, source_kind="manual", source_label="chat_synthesis",
            quote="An uncited chat answer restating the fact.", confidence=0.5,
        )
        self.conn.commit()
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(alice), "superseded")

    def test_genuine_manual_source_still_counts_as_support(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        alice = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = ?", (ALICE,)
        ).fetchone()[0]
        self.store.attach_source(
            claim_id=alice, source_kind="manual", source_label="vault-note",
            quote="Confirmed by the vault operator.", confidence=0.9,
        )
        self.conn.commit()
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(alice), "active")
        self.assertEqual(len(self._findings(alice, "weak_provenance")), 1)


class TestEscapedQuotePresence(_O04cBase):
    """PRR-006 / F-2: curator quotes stored XML-escaped are matched via the
    HTML-unescape arm — byte-identical recompiles keep them."""

    def test_escaped_quote_kept_on_identical_recompile(self):
        self.compiler.compile_ingest_job(
            1, {"file_id": 1, "text": "R&D at AFOMIS is critical. " + V1}
        )
        page = self._page_id()
        cid = self._seed(
            "research and development matters at afomis",
            quote="R&amp;D at AFOMIS is critical.",
            page_id=page,
            created_by_kind="llm_curator",
        )
        # Byte-identical recompile: the raw text carries "R&D", the stored
        # quote is the escaped echo — the unescape arm must keep the claim.
        self.compiler.compile_ingest_job(
            1, {"file_id": 1, "text": "R&D at AFOMIS is critical. " + V1}
        )
        self.assertEqual(self._status(cid), "active")
        self.assertEqual(len(self._findings(cid, "stale")), 0)
        # And when the sentence truly leaves, the claim supersedes.
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        self.assertEqual(self._status(cid), "superseded")


class TestTokenBoundaryPresence(_O04cBase):
    """F-5: normalized containment is token-padded — 'Director' must not
    match inside 'Directorate'."""

    def test_partial_word_quote_not_treated_as_present(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "alice smith directs afomis",
            quote="Alice Smith is the AFOMIS Director",
            page_id=page,
        )
        # "Director" is a prefix of "Directorate": without token padding the
        # quote would count as present and the claim would survive forever.
        self.compiler.compile_ingest_job(
            1, {"file_id": 1, "text": "Alice Smith is the AFOMIS Directorate head. " + V2}
        )
        self.assertEqual(self._status(cid), "superseded")

    def test_full_word_match_still_kept(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "alice smith directs afomis",
            quote="Alice Smith is the AFOMIS Director",
            page_id=page,
        )
        self.compiler.compile_ingest_job(
            1, {"file_id": 1, "text": "Alice Smith is the AFOMIS Director emerita. " + V2}
        )
        self.assertEqual(self._status(cid), "active")


class TestPunctuationOnlyQuoteUndecidable(_O04cBase):
    """F-6 / PRR-017 final semantics: a punctuation-only quote never matches
    anything textually, so its absence is NOT evidence of a content change —
    the claim is kept (undecidable), matching the docstring's fail-open."""

    def test_punctuation_only_quote_absent_keeps_claim(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "zoe quinn signs everything with dots...",
            quote="...",
            page_id=page,
        )
        result = self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(cid), "active")
        self.assertEqual(len(self._findings(cid, "stale")), 0)
        self.assertEqual(result.get("claims_superseded"), 1)  # Alice only

    def test_punctuation_only_quote_with_real_sibling_still_supersedes(self):
        """A real-content quote absent from the text is positive evidence even
        when a punctuation-only sibling row also exists."""
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "zoe quinn signs everything with dots...",
            quote="...",
            page_id=page,
        )
        self.store.attach_source(
            claim_id=cid, source_kind="document", file_id=1,
            source_label="file:1",
            quote="A real sentence that appears nowhere in any text.",
            confidence=0.8,
        )
        self.conn.commit()
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(cid), "superseded")


class TestWhitespaceTextFailOpen(_O04cBase):
    """F-7: whitespace-only current text carries no positive evidence — the
    pass fails open instead of mass-superseding."""

    def test_whitespace_only_text_supersedes_nothing(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        before = {
            tuple(r) for r in self.conn.execute(
                "SELECT id, status FROM wiki_claims"
            ).fetchall()
        }
        result = self.compiler.compile_ingest_job(1, {"file_id": 1, "text": "   "})
        self.assertEqual(result.get("claims_superseded"), 0)
        self.assertEqual(result.get("claims_weak_provenance"), 0)
        after = {
            tuple(r) for r in self.conn.execute(
                "SELECT id, status FROM wiki_claims"
            ).fetchall()
        }
        self.assertEqual(before, after)


class TestResultKeysSurfaceRetirement(_O04cBase):
    """PRR-016: the supersede counts surface in the compile result."""

    def test_result_reports_supersede_counts(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        result = self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(result.get("claims_superseded"), 1)
        self.assertEqual(result.get("claims_weak_provenance"), 0)
        result2 = self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(result2.get("claims_superseded"), 0)


class TestCompilerCreatedClaimPinnedSuperseded(_O04cBase):
    """PRR-009 companion: the frozen AC1 counts are complemented by an exact
    status pin on a compiler-created claim (pre-assert + exact transition)."""

    def test_alice_claim_exists_active_then_superseded(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        row = self.conn.execute(
            "SELECT id, status FROM wiki_claims WHERE claim_text = ?", (ALICE,)
        ).fetchone()
        self.assertIsNotNone(row, "v1 compile must create the Alice claim")
        self.assertEqual(dict(row)["status"], "active")
        alice = dict(row)["id"]
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(alice), "superseded")


class TestMemorySourceCountsAsSupport(_O04cBase):
    """PRR-011: the file_id IS NULL arm of the independent-support split —
    a memory-backed claim stays active with a weak_provenance finding."""

    def test_memory_backed_claim_stays_active(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "Zoe Quinn is the SIGMA Director.",
            quote="Zoe Quinn is the SIGMA Director.",
            page_id=page,
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO memories (id, vault_id, content) VALUES (7, 1, ?)",
            ("Zoe Quinn is the SIGMA Director.",),
        )
        self.store.attach_source(
            claim_id=cid, source_kind="memory", memory_id=7,
            source_label="memory:7", quote="Zoe Quinn is the SIGMA Director.",
            confidence=0.9,
        )
        self.conn.commit()
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(cid), "active")
        self.assertEqual(len(self._findings(cid, "weak_provenance")), 1)


class TestKeepByIdDiscriminating(_O04cBase):
    """PRR-012: a claim in the keep set whose stored quote is absent from the
    current text stays active — deleting the keep guard must fail this."""

    def test_keep_set_overrides_absent_quote(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "Zoe Quinn is the SIGMA Director.",
            quote="A sentence that appears nowhere in the texts.",
            page_id=page,
        )
        counts = self.store.supersede_unrederived_file_claims(
            file_id=1, vault_id=1, page_id=page,
            keep_claim_ids={cid}, current_text=V2,
        )
        self.assertEqual(self._status(cid), "active")
        # The V1-compile-derived Alice claim (quote absent from V2) is the
        # superseded one; the keep-protected claim is untouched.
        self.assertEqual(counts["superseded"], 1)
        # Same claim without the keep id: verifiably absent quote -> superseded.
        counts = self.store.supersede_unrederived_file_claims(
            file_id=1, vault_id=1, page_id=page,
            keep_claim_ids=set(), current_text=V2,
        )
        self.assertEqual(self._status(cid), "superseded")
        self.assertEqual(counts["superseded"], 1)


class TestFindingShapePinned(_O04cBase):
    """PRR-026 / F-10: finding row shape and the mixed sole/multi-source
    recompile (kills the weak-title-only mutant: two claims, two findings)."""

    def test_mixed_recompile_produces_both_findings_with_full_shape(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        sole = self._seed(
            "Zoe Quinn is the SIGMA Director.",
            quote="Zoe Quinn is the SIGMA Director.",
            page_id=page,
        )
        multi = self._seed(
            "Rosa Vega is the SIGMA Manager.",
            quote="Rosa Vega is the SIGMA Manager.",
            page_id=page,
        )
        self.store.attach_source(
            claim_id=multi, source_kind="document", file_id=2,
            source_label="file:2", quote="Rosa Vega is the SIGMA Manager.",
            confidence=0.8,
        )
        self.conn.commit()
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(sole), "superseded")
        self.assertEqual(self._status(multi), "active")
        stale = self._findings(sole, "stale")
        self.assertEqual(len(stale), 1)
        weak = self._findings(multi, "weak_provenance")
        self.assertEqual(len(weak), 1)
        s_row, w_row = dict(stale[0]), dict(weak[0])
        self.assertEqual(s_row["severity"], "medium")
        self.assertEqual(s_row["origin"], "invalidation")
        self.assertIn(f"claim_id={sole}", s_row["title"])
        self.assertIn("file_id=1", s_row["title"])
        self.assertEqual(s_row["related_page_ids_json"], json.dumps([page]))
        self.assertEqual(s_row["details"], "Zoe Quinn is the SIGMA Director.")
        self.assertEqual(w_row["severity"], "low")
        self.assertIn(f"claim_id={multi}", w_row["title"])

        # details truncation arm (PRR-026 sub-item): a claim_text longer than
        # 200 chars is truncated in the finding details.
        long_text = "Zoe Quinn signs document " + ("x" * 240)
        long = self._seed(long_text, quote="Zoe Quinn signs document " + "x" * 240,
                          page_id=page)
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(long), "superseded")
        long_rows = self._findings(long, "stale")
        self.assertEqual(len(long_rows), 1)
        self.assertEqual(dict(long_rows[0])["details"], long_text[:200])


class TestFailOpenVariants(_O04cBase):
    """PRR-027: NULL-stored quotes, whitespace-only quotes and the two-row
    any() shape all fail open (or keep via the present row)."""

    def _seed_two_rows(self, page_id, quote2):
        cid = self._seed(
            "Zoe Quinn is the SIGMA Director.",
            quote="Zoe Quinn is the SIGMA Director.",
            page_id=page_id,
        )
        self.store.attach_source(
            claim_id=cid, source_kind="document", file_id=1,
            source_label="file:1", quote=quote2, confidence=0.8,
        )
        self.conn.commit()
        return cid

    def test_null_quote_row_fails_open(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        claim = self.store.create_claim(
            vault_id=1, claim_text="Zoe Quinn is the SIGMA Director.",
            source_type="document", page_id=page,
        )
        cid = claim.id
        self.store.attach_source(
            claim_id=cid, source_kind="document", file_id=1,
            source_label="file:1", confidence=0.8,
        )
        self.conn.commit()
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(cid), "active")

    def test_whitespace_only_quote_row_fails_open(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "Zoe Quinn is the SIGMA Director.",
            quote="   ",
            page_id=page,
        )
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(cid), "active")

    def test_two_rows_any_present_keeps_claim(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed_two_rows(page, quote2="Carol Doe is the AFOMIS Deputy Director.")
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(cid), "active")

    def test_two_rows_both_absent_supersedes(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed_two_rows(page, quote2="A sentence absent from every text.")
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(cid), "superseded")


class TestReactivateHelperDirect(_O04cBase):
    """PRR-005 store-level: the no-commit reactivation helper flips a
    superseded claim and deletes only its OPEN stale findings."""

    def test_reactivate_claim_if_superseded(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        zoe = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = 'Zoe Quinn is the SIGMA Director.'"
        ).fetchone()[0]
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        self.assertEqual(self._status(zoe), "superseded")
        self.assertEqual(len(self._findings(zoe, "stale")), 1)
        flipped = self.store.reactivate_claim_if_superseded(zoe)
        self.assertTrue(flipped)
        # The helper is no-commit: the caller owns the transaction (here, an
        # explicit commit before any later rollback-triggering call).
        self.conn.commit()
        self.assertEqual(self._status(zoe), "active")
        self.assertEqual(len(self._findings(zoe, "stale")), 0)
        # Already-active: no-op, and its rollback does not undo the committed
        # flip above.
        self.assertFalse(self.store.reactivate_claim_if_superseded(zoe))
        self.assertEqual(self._status(zoe), "active")
        self.assertEqual(len(self._findings(zoe, "stale")), 0)

    def test_reactivate_leaves_user_resolved_rows_alone(self):
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": VT1})
        zoe = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = 'Zoe Quinn is the SIGMA Director.'"
        ).fetchone()[0]
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V2})
        fid = self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE related_claim_ids_json = ?",
            (f"[{zoe}]",),
        ).fetchone()[0]
        self.store.update_lint_finding(fid, 1, "acknowledged")
        self.store.reactivate_claim_if_superseded(zoe)
        self.assertEqual(len(self._findings(zoe, "stale", status="acknowledged")), 1)


class TestCuratorPathReactivatesSupersededClaim(_O04cBase):
    """PRR-005 / F-2 / F-4 integration: with the curator enabled and its
    client stubbed, an accepted candidate that re-emits a claim the supersede
    pass just retired must REACTIVATE it, not bury it. The stored claim's
    quote ("heads") is verifiably absent from the compiled text ("leads"), so
    the presence check supersedes it; the curator's candidate quote IS in the
    chunk text, so the persistence block finds the claim by text and the
    inline reactivation must fire."""

    def test_curator_reemission_reactivates_superseded_claim(self):
        sentence = "Dana Reed leads research at AFOMIS."
        self.compiler.compile_ingest_job(1, {"file_id": 1, "text": V1})
        page = self._page_id()
        cid = self._seed(
            "dana reed heads research at afomis",
            quote="Dana Reed heads research at AFOMIS.",
            page_id=page,
        )
        payload = json.dumps({"claims": [{
            "claim_text": "dana reed heads research at afomis",
            "claim_type": "fact",
            "subject": "Dana Reed",
            "predicate": "heads",
            "object": "research",
            "source_quote": sentence,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://api.example.com"
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
            result = self.compiler.compile_ingest_job(
                1, {"file_id": 1, "text": sentence + " " + V2}
            )
        self.assertEqual(self._status(cid), "active")
        self.assertEqual(len(self._findings(cid, "stale")), 0)
        self.assertEqual(result["curator"]["accepted"], 1)


