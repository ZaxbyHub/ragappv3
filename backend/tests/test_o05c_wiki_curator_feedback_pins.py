"""Feedback-round pins for issue #795 / PR #971 (swarm-pr-review findings).

Every test here pins a finding from the two independent reviews of head
31d8fc28 (orchestrator run + parallel run) that the frozen checks and the
o05b battery did not cover:

  - H-1: digit-run truncation at the alignment window edge (quote number
    a strict prefix of a longer source number) must NOT verify.
  - H-2: swapped associated numbers DO verify end-to-end at >= 92 when the
    chunk carries an extra number -- pinned as documented residual
    (the release fragment claimed this was not constructable; it is).
  - H-3: a needs_review curator candidate (draft mode + unknown chunk +
    fabricated quote) must NEVER reactivate a superseded claim.
  - H-4: the shared breaker's identity must not carry the operator URL.
  - F5: caller contextvars (correlation identity) survive the offload
    executor dispatch.
  - F6: the single-worker serialization premise, the always-offload
    dispatch, and the per-call prompt budget are all pinned.
  - Hygiene: 429 classifies as outage; the prompt still requests the
    consumed sections; breaker json() parse errors stay outside the wrap.
"""

import asyncio
import contextvars
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

import app.services.wiki_curator_client as wcc  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import run_migrations  # noqa: E402
from app.services.circuit_breaker import is_outage_status  # noqa: E402
from app.services.wiki_compiler import (  # noqa: E402
    _CURATOR_OFFLOAD_EXECUTOR,
    WikiCompiler,
)
from app.services.wiki_curator import (  # noqa: E402
    _CURATOR_SYSTEM_PROMPT,
    CuratorChunk,
    CuratorResult,
    _quote_matches,
)
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


class TestDigitRunTruncation(unittest.TestCase):
    """H-1: a window edge landing mid-digit-run must be expanded to the
    token boundary, so a prefix number never verifies against its own
    truncation. All shapes below executed True at 31d8fc28, False at base."""

    def test_truncating_shapes_rejected(self):
        self.assertFalse(
            _quote_matches(
                "The compaction window is 1215.",
                "The compaction window is 121563981 seconds and the "
                "tombstone ratio is 0.75",
            )
        )
        self.assertFalse(
            _quote_matches(
                "The retention period is 5838 and",
                "The retention period is 583868778 during the nightly "
                "compaction job",
            )
        )
        self.assertFalse(
            _quote_matches(
                "Renewal interval is 9023 so",
                "Renewal interval is 902326927 before the traffic peak begins",
            )
        )
        self.assertFalse(
            _quote_matches(
                "step 1: set timeout 50 ms",
                "step 1: set timeout 5000 ms. step 2: set timeout 500 ms.",
            )
        )

    def test_strict_substring_prefix_leak_is_pre_existing_out_of_scope(self):
        # NOT the window gate's concern: these quotes are verbatim
        # substrings of the source, so the strict path returns True before
        # the fuzzy gate runs. Pre-existing at base; documented residual.
        self.assertTrue(
            _quote_matches("The budget is 5000", "The budget is 50000 dollars")
        )
        self.assertTrue(
            _quote_matches("The ratio is 1.5", "The ratio is 1.55 for this cohort")
        )

    def test_windowed_non_truncating_quotes_still_accepted(self):
        # Non-regression: the C1 window shape and the quote-at-end fixture
        # (both frozen/battery-pinned) stay accepted under the expansion.
        self.assertTrue(
            _quote_matches(
                "The team completed 12 audits during 2024 per the annual report",
                "Page 7. The team completed 12 audits in 2024, per the "
                "annual report.",
            )
        )
        self.assertTrue(
            _quote_matches(
                "The session, lasts 30 minutes.",
                "Chapter 42 intro line. Section 99 overview. "
                "The session lasts 30 minutes.",
            )
        )


class TestSwappedNumbersDocumentedResidual(unittest.TestCase):
    """H-2: containment is order/position-blind; a swapped-number quote
    verifies end-to-end at >= 92 when the chunk carries an extra number.
    Pinned as a documented residual (the release fragment's earlier
    "not constructable at >= 92" claim was executed-false)."""

    def test_swap_accepted_is_pinned_residual(self):
        self.assertTrue(
            _quote_matches(
                "region us-east had 5 findings and region eu-west had 3 findings",
                "Page 12. region us-east had 3 findings and region eu-west "
                "had 5 findings.",
            )
        )


def _seed_superseded_claim(conn, store, claim_text, quote):
    claim = store.create_claim(
        vault_id=1, claim_text=claim_text, source_type="document",
        status="superseded",
    )
    store.attach_source(
        claim_id=claim.id, source_kind="document", file_id=1,
        chunk_id="1_0", source_label="file:1", quote=quote, confidence=0.8,
    )
    conn.commit()
    return claim.id


def _open_stale_finding(conn, claim_id):
    conn.execute(
        "INSERT INTO wiki_lint_findings (vault_id, finding_type, severity, "
        "title, status, origin, related_claim_ids_json) "
        "VALUES (1, 'stale', 'medium', 'stale probe', 'open', "
        "'invalidation', ?)",
        (f"[{claim_id}]",),
    )
    conn.commit()


def _claim_status(conn, claim_id):
    row = conn.execute(
        "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    return dict(row)["status"] if row else None


def _stale_finding_open(conn, claim_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM wiki_lint_findings "
        "WHERE finding_type = 'stale' AND status = 'open' "
        "AND related_claim_ids_json = ?",
        (f"[{claim_id}]",),
    ).fetchone()
    return dict(row)["n"]


class TestReactivationGate(unittest.TestCase):
    """H-3: a needs_review candidate must never flip a superseded claim."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.conn, self.store, self.compiler, self._td = _make_env()
        self.addCleanup(self._flag.__exit__, None, None, None)
        self.addCleanup(lambda: None)

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
                1, {"file_id": 1, "text": "Alpha is verified. Filler text."}
            )

    def test_draft_mode_unknown_chunk_fabricated_quote_no_reactivation(self):
        cid = _seed_superseded_claim(
            self.conn, self.store,
            "Alpha is verified", "Alpha is verified",
        )
        _open_stale_finding(self.conn, cid)
        settings.wiki_llm_curator_mode = "draft"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = False
        payload = json.dumps({"claims": [{
            "claim_text": "Alpha is verified",
            "claim_type": "fact",
            "source_quote": "Fabricated quote that appears nowhere 12345",
            "chunk_id": "bogus",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(payload)
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(_claim_status(self.conn, cid), "superseded")
        self.assertEqual(_stale_finding_open(self.conn, cid), 1)

    def test_verified_active_candidate_still_reactivates(self):
        cid = _seed_superseded_claim(
            self.conn, self.store,
            "Alpha is verified", "Alpha is verified",
        )
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        payload = json.dumps({"claims": [{
            "claim_text": "Alpha is verified",
            "claim_type": "fact",
            "source_quote": "Alpha is verified",
            "chunk_id": "1_0",
            "confidence": 0.9,
        }]})
        result = self._run_curator_payload(payload)
        self.assertEqual(result["curator"]["accepted"], 1)
        self.assertEqual(_claim_status(self.conn, cid), "active")


class _NoUrlTransport:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *_a, **_k):
        raise wcc.httpx.ConnectError("connection refused (pins)")


class TestBreakerIdentity(unittest.TestCase):
    """H-4: the breaker identity (and everything derived from it that
    reaches result_json) must not embed the operator URL."""

    def setUp(self):
        wcc._reset_shared_breakers()
        self.addCleanup(wcc._reset_shared_breakers)

    def test_open_breaker_message_carries_no_url(self):
        with patch.object(wcc.httpx, "AsyncClient", _NoUrlTransport), \
             patch.object(wcc, "assert_curator_url_safe", lambda url: None):
            client = wcc.CuratorClient(
                base_url="https://secret-gw.corp.internal:11434/v1",
                model="m",
            )
            self.assertNotIn("http", client._breaker.name)
            last = None
            for _ in range(7):
                try:
                    asyncio.run(client.propose([{"role": "user", "content": "x"}]))
                except Exception as e:  # noqa: BLE001 - recorded
                    last = e
            self.assertIsInstance(last, wcc.CircuitBreakerError)
            self.assertNotIn("http", str(last))
            self.assertNotIn("secret-gw", str(last))


_probe_var = contextvars.ContextVar("o05c-probe", default="unset")


class TestContextSurvivesDispatch(unittest.TestCase):
    """F5: caller contextvars must survive the offload dispatch so
    correlation headers keep the request identity."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.addCleanup(self._flag.__exit__, None, None, None)

    def test_contextvar_reaches_propose_on_worker_thread(self):
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"
        compiler = WikiCompiler.__new__(WikiCompiler)
        compiler._db = MagicMock()
        compiler._store = MagicMock()

        recorded = {}

        class _RecordingClient:
            def __init__(self, *a, **k):
                pass

            async def propose(self, *_a, **_k):
                # Mirrors real propose: correlation headers are built at
                # call time on whatever thread/loop executes the request.
                recorded["headers"] = wcc._correlation_headers()
                return json.dumps({"claims": []})

        class _StubCurator:
            def __init__(self, client, store=None):
                self.client = client

            async def curate(self, **_kwargs):
                await self.client.propose([{"role": "user", "content": "x"}])
                return CuratorResult()

        _probe_var.set("request-identity-123")
        with patch(
            "app.services.wiki_compiler.CuratorClient", _RecordingClient
        ), patch(
            "app.services.wiki_compiler.WikiCurator", _StubCurator
        ), patch(
            "app.services.wiki_curator_client._correlation_headers",
            lambda: {"x-probe": _probe_var.get()},
        ):
            compiler._maybe_run_curator(
                vault_id=1,
                file_id=1,
                trigger="manual",
                deterministic_extraction=types.SimpleNamespace(
                    acronyms=[], persons=[], role_claims=[]
                ),
                existing_entities=[],
                text="Alpha is verified.",
                page_id=None,
            )
        self.assertEqual(
            recorded["headers"].get("x-probe"), "request-identity-123"
        )


class TestSerializationPremises(unittest.TestCase):
    """F6: the single-worker premise, the always-offload dispatch, and the
    per-call prompt budget are pinned (all three mutations executed green
    on 31d8fc28 before these tests existed)."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.addCleanup(self._flag.__exit__, None, None, None)

    def test_offload_executor_is_single_worker(self):
        self.assertEqual(_CURATOR_OFFLOAD_EXECUTOR._max_workers, 1)

    def test_sync_dispatch_goes_through_offload_executor(self):
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"
        compiler = WikiCompiler.__new__(WikiCompiler)
        compiler._db = MagicMock()
        compiler._store = MagicMock()

        async def _fake_curate(**_kwargs):
            return CuratorResult()

        with patch(
            "app.services.wiki_compiler.CuratorClient"
        ), patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor, patch(
            "app.services.wiki_compiler._CURATOR_OFFLOAD_EXECUTOR",
            wraps=_CURATOR_OFFLOAD_EXECUTOR,
        ) as spy:
            cur_ctor.return_value.curate = _fake_curate
            # Plain sync context: no running event loop in this thread --
            # exactly the production shape (asyncio.to_thread callers).
            compiler._maybe_run_curator(
                vault_id=1,
                file_id=1,
                trigger="manual",
                deterministic_extraction=types.SimpleNamespace(
                    acronyms=[], persons=[], role_claims=[]
                ),
                existing_entities=[],
                text="Alpha is verified.",
                page_id=None,
            )
        self.assertTrue(spy.submit.called)

    def test_build_user_prompt_bounds_chunk_snippet(self):
        from app.services.wiki_curator import build_user_prompt

        prompt = build_user_prompt(
            [CuratorChunk(chunk_id="1_0", source_text="x" * 10000, file_id=1)]
        )
        self.assertNotIn("x" * 7000, prompt)
        self.assertIn("x" * 6000, prompt)


class TestHygiene(unittest.TestCase):
    def test_429_is_outage(self):
        self.assertTrue(is_outage_status(429))
        self.assertTrue(is_outage_status(500))
        self.assertFalse(is_outage_status(404))

    def test_prompt_requests_all_consumed_sections(self):
        for section in ('"claims"', '"open_questions"', '"contradictions"'):
            self.assertIn(section, _CURATOR_SYSTEM_PROMPT)

    def test_non_json_200_does_not_count_toward_fail_max(self):
        wcc._reset_shared_breakers()
        self.addCleanup(wcc._reset_shared_breakers)

        class _NonJsonTransport:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **_k):
                request = wcc.httpx.Request("POST", url)
                return wcc.httpx.Response(
                    200, request=request, content=b"<html>gateway</html>"
                )

        with patch.object(wcc.httpx, "AsyncClient", _NonJsonTransport), \
             patch.object(wcc, "assert_curator_url_safe", lambda url: None):
            client = wcc.CuratorClient(
                base_url="http://10.10.10.20:1/v1", model="m"
            )
            for _ in range(6):
                with self.assertRaises(ValueError):
                    asyncio.run(
                        client.propose([{"role": "user", "content": "x"}])
                    )
            self.assertEqual(client._breaker.fail_counter, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
