"""Review-round regression pins for the wave-6 runtime fixes (PR #1113,
swarm-pr-review pr1113-20261111). These live OUTSIDE the frozen check
files: they pin the two behavior changes the feedback round shipped after
the frozen checkpoint, each red on the pre-fix code.

- PRR-001: the lint scan runs on the DEDICATED bounded executor
  (``_WIKI_LINT_EXECUTOR``, thread_name_prefix='wiki-lint'), not on the
  shared default executor. RED if the dispatch reverts to bare
  ``asyncio.to_thread`` (default-executor worker threads do not carry the
  wiki-lint prefix), GREEN at head. This is the committed form of the
  review round's transient probe — without it, silently reverting the
  executor passed every durable check.
- PRR-002: ``_cap`` preserves ``None`` as ``None`` so absent optional
  contradiction fields persist as JSON null, exactly as they did before
  the cap (base stored raw ``c.get(...)`` values). RED if ``_cap`` goes
  back to ``str(value)`` (which stores a truthy literal "None" string),
  GREEN at head.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
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
    from app.services.wiki_compiler import WikiCompiler
    from app.services.wiki_store import WikiStore

    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)
    return conn, td, store, compiler


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


class TestLintRunsOnDedicatedExecutor(unittest.TestCase):
    """PRR-001: the scan executes on a wiki-lint-prefixed worker thread."""

    def test_run_wiki_lint_uses_dedicated_executor_thread(self):
        from app.api.routes import wiki as routes

        conn, td, _store, _compiler = _make_env()
        # Temp-hygiene (round 2, N-07): registered BEFORE close so LIFO
        # runs close first and rmtree last (Windows open-file lock).
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        recorded = {}

        class _StubLinter:
            def __init__(self, db, store):
                pass

            def run_lint(self, vault_id):
                recorded["thread_name"] = threading.current_thread().name
                return []

        import asyncio

        async def _evaluate(*_a, **_k):
            return True

        async def _call():
            return await routes.run_wiki_lint(
                request=types.SimpleNamespace(vault_id=1),
                db=conn,
                user={"id": 1},
                evaluate=_evaluate,
                _csrf_token="x",
            )

        with patch.object(routes, "WikiLinter", _StubLinter):
            response = asyncio.run(_call())
        self.assertEqual(response["count"], 0)
        # The scan must have actually run (harness guard).
        self.assertIn("thread_name", recorded)
        # Discriminating assertion: the dedicated executor's workers carry
        # the wiki-lint prefix. The shared default executor (bare
        # asyncio.to_thread) does not name its workers this way, so a
        # revert turns this red.
        self.assertTrue(
            recorded["thread_name"].startswith("wiki-lint"),
            f"run_lint must execute on the dedicated wiki-lint executor "
            f"(got worker thread {recorded['thread_name']!r} — a bare "
            f"asyncio.to_thread revert names default-pool workers "
            f"differently)",
        )


class TestCapPreservesNoneAsNull(unittest.TestCase):
    """PRR-002: absent optional contradiction fields persist as JSON null."""

    def test_absent_contradiction_field_persists_as_null(self):
        from app.services.wiki_curator import _cap

        # Unit-level discriminator first: _cap(None) must be None, not the
        # literal string "None" (the str()-only implementation returns
        # "None", which json.dumps stores as a truthy string).
        self.assertIsNone(
            _cap(None),
            "_cap must preserve None so absent fields persist as JSON "
            "null (str(None) would store a truthy 'None' string)",
        )
        # The persisted-shape oracle (a compile whose contradiction omits
        # the field, asserting the stored details JSON carries null) lives
        # in TestCapStoredRowShape below — this unit assert is the fast
        # discriminator; the stored-row test is the end-to-end proof.


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

    def __init__(self):
        self._snap = {}

    def __enter__(self):
        self._snap = {f: getattr(settings, f) for f in _FLAG_FIELDS}
        return self

    def __exit__(self, *exc):
        for f, v in self._snap.items():
            setattr(settings, f, v)
        return False


_MAX_DETAIL_CHARS = 500


class TestCapStoredRowShape(unittest.TestCase):
    """FB-004/FB-005 (review round 2): the cap contract at the PERSISTENCE
    seam - absent contradiction fields persist as JSON null, and a
    schema-drifting NON-string field is stringified and capped (the
    fragment's str()-safety claim had no test). Both drive the real
    compile path (stub CuratorClient, compile_ingest_job)."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.conn, self._td, self.store, self.compiler = _make_env()
        # Temp-hygiene (delta-gate r8): registered FIRST so LIFO runs it
        # LAST, after the connection is closed (Windows open-file lock).
        self.addCleanup(shutil.rmtree, self._td, True)
        self.addCleanup(self._flag.__exit__, None, None, None)
        self.addCleanup(self.conn.close)
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_mode = "draft"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True

    def _compile(self, payload):
        class _Stub:
            def __init__(self, *a, **k):
                pass

            async def propose(self, *_a, **_k):
                return payload

        with patch("app.services.wiki_compiler.CuratorClient", _Stub):
            return self.compiler.compile_ingest_job(
                1, {"file_id": 1, "text": "Alpha is pinned. Filler."}
            )

    def _contradiction_rows(self):
        return self.conn.execute(
            "SELECT details FROM wiki_lint_findings "
            "WHERE origin = 'curator' AND finding_type = 'contradiction'"
        ).fetchall()

    def test_absent_contradiction_fields_persist_as_null(self):
        # FB-005: a model payload OMITTING optional contradiction fields
        # must persist them as JSON null (the pre-cap base shape), not the
        # truthy literal string "None". RED under a str()-only _cap.
        payload = json.dumps({
            "claims": [],
            "contradictions": [{
                "claim_b": "Beta claims otherwise",
            }],
        })
        result = self._compile(payload)
        self.assertEqual(result["curator"]["lint"], 1)
        rows = self._contradiction_rows()
        self.assertEqual(len(rows), 1)
        details = json.loads(rows[0]["details"])
        self.assertIsNone(
            details["claim_a"],
            "an absent contradiction field must persist as JSON null "
            "(str(None) would store a truthy 'None' string)",
        )
        self.assertIsNone(details["reason"])
        self.assertIsNone(details["source_quote"])

    def test_non_string_contradiction_field_is_stringified_and_capped(self):
        # FB-004: a schema-drifting NON-string model value must be
        # stringified and capped, not TypeError the slice (the fragment's
        # str()-safety claim). RED if the str() guard is dropped.
        payload = json.dumps({
            "claims": [],
            "contradictions": [{
                "claim_a": {"nested": "D" * 900},
                "claim_b": 12345,
                "reason": "R" * 900,
                "source_quote": "Q" * 900,
            }],
        })
        result = self._compile(payload)
        self.assertEqual(result["curator"]["lint"], 1)
        rows = self._contradiction_rows()
        self.assertEqual(len(rows), 1)
        details = json.loads(rows[0]["details"])
        for key in ("claim_a", "claim_b", "reason", "source_quote"):
            self.assertIsInstance(
                details[key], str,
                "non-string model value for %r must be stringified" % key,
            )
            self.assertLessEqual(
                len(details[key]), _MAX_DETAIL_CHARS,
                "stringified %r must be capped at 500" % key,
            )


class TestLifespanShutsDownLintExecutor(unittest.TestCase):
    """FB-001 / review-round N-05a: the module-level wiki lint executor is
    shut down at app shutdown (repo convention: _auth_executor,
    redis_io.shutdown_executor). Source-inspection pin per the repo's
    wiring-guard convention (test_lifespan_pool_seeding_order precedent) -
    a runtime lifespan test would boot the whole app stack."""

    def test_lifespan_source_shuts_down_wiki_lint_executor(self):
        import app.lifespan as lifespan_module

        source = Path(lifespan_module.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "from app.api.routes.wiki import _WIKI_LINT_EXECUTOR",
            source,
            "lifespan shutdown must import the wiki lint executor",
        )
        self.assertIn(
            "_WIKI_LINT_EXECUTOR.shutdown(wait=False)",
            source,
            "lifespan shutdown must shut the executor down (wait=False)",
        )


class TestPromotionNumericIdentityGate(unittest.TestCase):
    """F-03 (OOB review, critic-executed end to end): the normalized lookup
    strips punctuation, so a quote verified for "The limit is 5.10 MB"
    must NOT promote an existing unverified "The limit is 510 MB" — the
    promotion requires equal numeric multisets. Punctuation-only variants
    (same numbers) still promote."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.conn, self._td, self.store, self.compiler = _make_env()
        self.addCleanup(self._flag.__exit__, None, None, None)
        self.addCleanup(self.conn.close)
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True

    def _seed_unverified(self, text):
        claim = self.store.create_claim(
            vault_id=1,
            claim_text=text,
            source_type="document",
            page_id=None,
            status="unverified",
            created_by_kind="deterministic",
        )
        return claim.id

    def _compile_payload(self, claim_text):
        payload = json.dumps({"claims": [{
            "claim_text": claim_text,
            "claim_type": "fact",
            "source_quote": claim_text,
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})

        class _Stub:
            def __init__(self, *a, **k):
                pass

            async def propose(self, *_a, **_k):
                return payload

        with patch("app.services.wiki_compiler.CuratorClient", _Stub):
            return self.compiler.compile_ingest_job(
                1, {"file_id": 1, "text": f"{claim_text}. Filler text."}
            )

    def _status(self):
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE claim_text LIKE ?",
            ("The fee is%",),
        ).fetchone()
        return dict(row)["status"] if row else None

    def test_numeric_mismatch_blocks_promotion(self):
        # "The fee is 5.10 USD" normalizes equal to "The fee is 510 USD"
        # (punctuation stripped) — the numeric gate must block the lift.
        cid = self._seed_unverified("The fee is 510 USD")
        result = self._compile_payload("The fee is 5.10 USD")
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(
            self._status(), "unverified",
            "a quote verified for different numbers must NOT promote the "
            "matched claim (5.10 vs 510 collide under normalize)",
        )

    def test_numeric_identical_variant_promotes(self):
        # Control: punctuation-only variant with the SAME numbers promotes.
        cid = self._seed_unverified("The fee is 510 USD")
        result = self._compile_payload("The fee is 510 USD!")
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(
            self._status(), "active",
            "punctuation-only variant with identical numbers promotes",
        )

    def test_non_numeric_texts_promote(self):
        # Control: no numeric tokens on either side -> multisets equal.
        self.store.create_claim(
            vault_id=1,
            claim_text="Delta is pinned",
            source_type="document",
            page_id=None,
            status="unverified",
            created_by_kind="deterministic",
        )
        settings.wiki_llm_curator_mode = "active_if_verified"
        result = self._compile_payload("Delta is pinned")
        self.assertEqual(result["curator"]["accepted"], 1)
        row = self.conn.execute(
            "SELECT status FROM wiki_claims WHERE claim_text = 'Delta is pinned'"
        ).fetchone()
        self.assertEqual(dict(row)["status"], "active")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
