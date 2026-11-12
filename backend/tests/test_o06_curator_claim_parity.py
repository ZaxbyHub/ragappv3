"""Curator claim-parity checks for the collated wiki lint lifecycle trace
(issues #929 + #932 + #935).

Harness mirrors backend/tests/test_o05c_wiki_curator_feedback_pins.py:
``_make_env`` (temp DB + run_migrations + vault/files seed), the
``_FlagSnapshot`` settings guard, and the ``_run_curator_payload`` pattern
(stub CuratorClient, ``compile_ingest_job(1, {"file_id": 1, "text": ...})``;
chunk id "1_0"). Checks in this file:

- C1 (AC1, DISCRIMINATING): in ``active_if_verified`` mode a quote-verified
  accepted claim that matches an existing ``unverified`` claim by text must
  PROMOTE it to ``active`` — the exact parity the deterministic twin
  ``WikiCompiler._find_or_create_claim`` already has for the unverified →
  active transition (its superseded → active reactivation twin is pinned by
  o05c's ``test_verified_active_candidate_still_reactivates``). At base the
  curator persistence block only reacts ``superseded`` → ``active``, so the
  matched claim stays ``unverified``. RED at base, GREEN after the fix.
- C3 (AC3, PRESERVING): a draft-mode curator rerun
  (``accepted.status == 'needs_review'``) must leave a matched claim's
  status unchanged — here the UNVERIFIED-match arm (the superseded-draft
  arm is pinned by o05c's
  ``test_draft_mode_unknown_chunk_fabricated_quote_no_reactivation``).
  GREEN at base (pin).
- C4 (AC4, PRESERVING): ``WikiStore.update_lint_finding`` accepts exactly
  the user statuses resolved/dismissed/acknowledged and still rejects the
  system-only ``auto_resolved`` (the rejection arm is pinned by o01's
  ``test_update_lint_finding_rejects_system_only_status``; this file pins
  the acceptance arms so the fix cannot "tighten" the contract).
  GREEN at base (pin).
"""

import json
import os
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


_CLAIM_TEXT = "Alpha is verified"


def _seed_unverified_claim(store, source_type="document"):
    claim = store.create_claim(
        vault_id=1,
        claim_text=_CLAIM_TEXT,
        source_type=source_type,
        page_id=None,
        status="unverified",
        created_by_kind="deterministic",
    )
    return claim.id


def _claim_status(conn, claim_id):
    row = conn.execute(
        "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    return dict(row)["status"] if row else None


def _document_source_count(conn, claim_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM wiki_claim_sources "
        "WHERE claim_id = ? AND source_kind = 'document' AND file_id = 1",
        (claim_id,),
    ).fetchone()
    return dict(row)["n"]


class TestCuratorClaimParity(unittest.TestCase):
    """C1 (DISCRIMINATING) + C3 (PRESERVING): the curator's find-or-match
    persistence must promote a matched unverified claim on a verified
    active candidate (C1 — RED at base, GREEN after the fix) while a
    needs_review draft candidate attaches provenance WITHOUT touching the
    matched claim's status (C3 — GREEN at base)."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.conn, self.store, self.compiler, self._td = _make_env()
        self.addCleanup(self._flag.__exit__, None, None, None)

    def tearDown(self):
        self.conn.close()

    def _run_curator_payload(self, payload):
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"

        class _Stub:
            def __init__(self, *a, **k):
                pass

            async def propose(self, *_a, **_k):
                return payload

        with patch("app.services.wiki_compiler.CuratorClient", _Stub):
            return self.compiler.compile_ingest_job(
                1, {"file_id": 1, "text": f"{_CLAIM_TEXT}. Filler text."}
            )

    def test_active_if_verified_promotes_unverified_match(self):
        # check: C1 (AC1, DISCRIMINATING) — RED at base, GREEN after the fix.
        cid = _seed_unverified_claim(self.store)
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        payload = json.dumps({"claims": [{
            "claim_text": _CLAIM_TEXT,
            "claim_type": "fact",
            "source_quote": _CLAIM_TEXT,   # verbatim in chunk "1_0"
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(payload)
        self.assertEqual(result["curator"]["accepted"], 1)
        # Guard: the claim the curator matched is the seeded one (not a
        # fresh duplicate row), so the status assert below reads the claim
        # the persistence branch actually found by text.
        rows = self.conn.execute(
            "SELECT id FROM wiki_claims WHERE claim_text = ?", (_CLAIM_TEXT,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        # Discriminating assertion: a quote-verified active candidate must
        # promote the matched unverified claim — the deterministic twin
        # (_find_or_create_claim) already does this. At base the claim
        # stays 'unverified'. RED at base, GREEN after the fix.
        self.assertEqual(
            _claim_status(self.conn, cid),
            "active",
            "quote-verified active candidate must promote a matched "
            "'unverified' claim to 'active' (deterministic-path parity)",
        )
        # Provenance + column-lift pins (plan-critic round 1, finding 5):
        # AC1 requires ATTACH THE SOURCE AND promote — a status-only
        # promotion must fail here. The deterministic twin also lifts
        # source_type and confidence onto the matched claim; pin the
        # confidence lift (0.9 from the verified candidate over the
        # deterministic default) and the document-source attachment.
        self.assertGreaterEqual(
            _document_source_count(self.conn, cid), 1,
            "promotion must attach the curator's document provenance",
        )
        lifted = self.conn.execute(
            "SELECT confidence FROM wiki_claims WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual(
            dict(lifted)["confidence"], 0.9,
            "promotion must lift the verified candidate's confidence onto "
            "the matched claim (deterministic-path parity)",
        )
        # Document-evidence arm (implementation-review round 1): this
        # candidate carries file_id=1 (ingest trigger), so the promotion
        # lifts source_type to 'document' exactly like the deterministic
        # twin; a file_id-less candidate (manual/query trigger) must
        # PRESERVE the matched claim's own source_type instead of
        # relabeling memory-sourced provenance.
        typed = self.conn.execute(
            "SELECT source_type FROM wiki_claims WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual(
            dict(typed)["source_type"], "document",
            "document-backed promotion must lift source_type to 'document'",
        )

    def test_active_mode_lifts_document_source_type_from_memory_seed(self):
        # Implementation-review round 2: the source_type pin must be
        # DISCRIMINATING — seeding 'document' made the original assert
        # unfailable. This arm seeds a MEMORY-sourced unverified claim and
        # requires the document-backed (ingest-trigger, file_id=1)
        # promotion to LIFT source_type to 'document'. Kills the
        # preserve-mutant (gate replaced by existing.source_type) and the
        # no-op-mutant (lift removed). RED at base (no promotion at all),
        # GREEN after the fix.
        cid = _seed_unverified_claim(self.store, source_type="memory")
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        payload = json.dumps({"claims": [{
            "claim_text": _CLAIM_TEXT,
            "claim_type": "fact",
            "source_quote": _CLAIM_TEXT,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(payload)
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(_claim_status(self.conn, cid), "active")
        typed = self.conn.execute(
            "SELECT source_type FROM wiki_claims WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual(
            dict(typed)["source_type"], "document",
            "document-backed promotion must LIFT a memory-sourced claim's "
            "source_type to 'document' (deterministic-path parity)",
        )

    def test_manual_trigger_preserves_memory_source_type(self):
        # Implementation-review round 2, preserve arm: on the manual
        # trigger (file_id=None — the curator holds no document), the
        # promotion must PRESERVE the matched claim's own source_type
        # instead of relabeling memory provenance as 'document'. Kills the
        # unconditional-'document' mutant. RED at base (no promotion),
        # GREEN after the fix.
        cid = _seed_unverified_claim(self.store, source_type="memory")
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        settings.wiki_llm_curator_run_on_manual = True
        payload = json.dumps({"claims": [{
            "claim_text": _CLAIM_TEXT,
            "claim_type": "fact",
            "source_quote": _CLAIM_TEXT,
            "chunk_id": "text_0",
            "confidence": 0.9,
        }]})

        class _Stub:
            def __init__(self, *a, **k):
                pass

            async def propose(self, *_a, **_k):
                return payload

        _ns = types.SimpleNamespace(role_claims=[], acronyms=[])
        with patch("app.services.wiki_compiler.CuratorClient", _Stub):
            summary = self.compiler._maybe_run_curator(
                vault_id=1,
                file_id=None,
                text=f"{_CLAIM_TEXT}. Filler text.",
                trigger="manual",
                deterministic_extraction=_ns,
                page_id=None,
                existing_entities=[],
            )
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(
            _claim_status(self.conn, cid), "active",
            "quote-verified manual-trigger candidate must still promote "
            "the matched claim",
        )
        typed = self.conn.execute(
            "SELECT source_type FROM wiki_claims WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual(
            dict(typed)["source_type"], "memory",
            "a file_id-less (manual/query) promotion must PRESERVE the "
            "matched claim's source_type — memory provenance is never "
            "relabeled as document",
        )

    def test_active_mode_promotes_normalized_text_match(self):
        # AC1 arm (plan-critic round 1, finding 5): the find-or-create
        # matches by normalized text when the exact text misses, so the
        # promotion must fire for a punctuation/whitespace variant too.
        # RED at base, GREEN after the fix.
        cid = _seed_unverified_claim(self.store)
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        payload = json.dumps({"claims": [{
            # Normalized-EQUIVALENT but textually-DIFFERENT variant: the
            # trailing exclamation survives the curator's strip() (so the
            # exact-text lookup misses) while normalize_claim_text strips
            # punctuation (so the normalized lookup hits). A pure
            # whitespace variant would NOT work here — the curator strips
            # whitespace before the lookup, restoring exact equality
            # (final-critic round 1).
            "claim_text": f"{_CLAIM_TEXT}!",
            "claim_type": "fact",
            "source_quote": _CLAIM_TEXT,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(payload)
        self.assertEqual(result["curator"]["accepted"], 1)
        rows = self.conn.execute(
            "SELECT id FROM wiki_claims", ()
        ).fetchall()
        self.assertEqual(len(rows), 1, "normalized match must not create a duplicate")
        self.assertEqual(
            _claim_status(self.conn, cid), "active",
            "normalized-text match must promote the matched unverified claim",
        )

    def test_attach_failure_rolls_back_promotion(self):
        # AC1 arm (plan-critic round 1, finding 5): the promotion is
        # commit-free inside the claim-pair SAVEPOINT — when the source
        # attachment fails for the SAME pair, the savepoint must roll the
        # promotion back with it (pair atomicity). GREEN at base (nothing
        # promotes at base), GREEN after the fix.
        cid = _seed_unverified_claim(self.store)
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        payload = json.dumps({"claims": [{
            "claim_text": _CLAIM_TEXT,
            "claim_type": "fact",
            "source_quote": _CLAIM_TEXT,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        with patch.object(
            self.store, "attach_source",
            side_effect=RuntimeError("injected attach failure"),
        ):
            result = self._run_curator_payload(payload)
        # The per-claim savepoint swallowed the failure into the summary...
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertTrue(
            any("persist_error" in e for e in result["curator"]["errors"]),
            "the injected attach failure must be quarantined into the "
            "curator summary errors, not raised",
        )
        # ...and the pair rolled back together: no promotion, no source.
        self.assertEqual(
            _claim_status(self.conn, cid), "unverified",
            "a failed attach must roll the promotion back with the pair "
            "(savepoint atomicity)",
        )
        self.assertEqual(_document_source_count(self.conn, cid), 0)

    def test_draft_mode_leaves_unverified_match_unchanged(self):
        # check: C3 (AC3, PRESERVING) — GREEN at base (pin).
        cid = _seed_unverified_claim(self.store)
        settings.wiki_llm_curator_mode = "draft"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        payload = json.dumps({"claims": [{
            "claim_text": _CLAIM_TEXT,
            "claim_type": "fact",
            "source_quote": _CLAIM_TEXT,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(payload)
        self.assertEqual(result["curator"]["accepted"], 1)
        # A needs_review (draft) candidate must NEVER change the matched
        # claim's status — same gate the reactivation branch uses.
        self.assertEqual(_claim_status(self.conn, cid), "unverified")
        # ... but it still attaches its provenance (document source for
        # file 1) without the status flip.
        self.assertGreaterEqual(_document_source_count(self.conn, cid), 1)


class TestUpdateLintFindingStatusContract(unittest.TestCase):
    """C4 (AC4, PRESERVING): the user-settable statuses stay exactly
    resolved/dismissed/acknowledged (all accepted), and the system-only
    'auto_resolved' stays rejected. GREEN at base (pin)."""

    def setUp(self):
        self.conn, self.store = _make_env()[:2]

    def tearDown(self):
        self.conn.close()

    def test_update_lint_finding_accepts_user_statuses_rejects_auto_resolved(self):
        for status in ("resolved", "dismissed", "acknowledged"):
            self.store.create_lint_finding(
                vault_id=1, finding_type="orphan",
                title=f"Status contract probe {status}",
            )
            row = self.conn.execute(
                "SELECT id FROM wiki_lint_findings "
                "WHERE title = ?",
                (f"Status contract probe {status}",),
            ).fetchone()
            finding = self.store.update_lint_finding(row["id"], 1, status)
            self.assertIsNotNone(finding, f"{status} update must return the finding")
            self.assertEqual(
                finding.status, status,
                f"{status} must remain a user-settable lint status",
            )
            stored = self.conn.execute(
                "SELECT status FROM wiki_lint_findings WHERE id = ?",
                (row["id"],),
            ).fetchone()["status"]
            self.assertEqual(stored, status)

        # The system-only status stays rejected (o01 pins the same arm;
        # duplicated here so the fix cannot tighten OR loosen the set).
        self.store.create_lint_finding(
            vault_id=1, finding_type="orphan", title="System-only probe o06"
        )
        row = self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE title = 'System-only probe o06'"
        ).fetchone()
        with self.assertRaises(ValueError):
            self.store.update_lint_finding(row["id"], 1, "auto_resolved")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
