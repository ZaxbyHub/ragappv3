"""Non-frozen unit battery for issue #795 (workstream O PR 5).

Companion to the frozen backend/tests/test_o05_wiki_curator_gates.py
(arm's-length authored). Covers the edge-case rows of the approved plan
(.agents/issue-traces/795-wiki-curator-quote-gating-breaker/07-approved-plan.md)
that the frozen checks do not isolate:

  - _quote_matches: whitespace-only hardening, threshold gate, numeric
    multiplicity, window containment (mutation rejection + the documented
    containment boundary), quote-at-end-of-numeric-chunk discriminator
    (bites the src/dest slicing bug the plan critic found), whole-file
    verify_quote operand (the PUT /wiki/claims re-verification shape).
  - active_if_verified status matrix: flag-off unknown chunk kept
    needs_review with its OWN cited id (never fabricated chunks[0]
    provenance), empty chunk_id, flag-on rejection, flag-off known-chunk
    activation, draft-mode pin.
  - shared breaker registry: endpoint keying/isolation, OPS-002
    non-outage 404 not counting, outage 500 counting, and the
    two-concurrent-event-loops boundary.
  - truncation-inside-open_questions residual pin (json_parse_failed
    wholesale rejection is unchanged; removing entities/relations from
    the prompt shrank, not eliminated, the loss window).
  - prompt no longer requests page_title/page_type.
"""

import asyncio
import json
import os
import sys
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

import app.services.wiki_curator_client as wcc  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.wiki_curator import (  # noqa: E402
    _CURATOR_SYSTEM_PROMPT,
    CuratorChunk,
    WikiCurator,
    _numeric_multiset,
    _numeric_submultiset,
    _quote_matches,
    verify_quote,
)
from app.services.wiki_curator_client import CuratorClient  # noqa: E402


def _snapshot_flags():
    return {
        f: getattr(settings, f)
        for f in (
            "wiki_llm_curator_mode",
            "wiki_llm_curator_require_quote_match",
            "wiki_llm_curator_require_chunk_id",
        )
    }


def _restore_flags(snap):
    for f, v in snap.items():
        setattr(settings, f, v)


class _StubCuratorClient:
    """Returns a canned JSON payload (or raw text) from propose()."""

    def __init__(self, payload=None, raw=None):
        self._payload = payload
        self._raw = raw

    async def propose(self, *_a, **_k):
        if self._raw is not None:
            return self._raw
        return json.dumps(self._payload)


def _claim(chunk_id, quote, text="Alpha is verified"):
    return {
        "claim_text": text,
        "claim_type": "fact",
        "source_quote": quote,
        "chunk_id": chunk_id,
        "confidence": 0.9,
    }


class TestQuoteMatchesWindows(unittest.TestCase):
    def test_whitespace_only_quote_and_source_rejected(self):
        src = "Alpha beta gamma."
        self.assertFalse(_quote_matches("   ", src))
        self.assertFalse(_quote_matches("", src))
        self.assertFalse(_quote_matches("alpha beta", "   "))
        self.assertFalse(_quote_matches("alpha beta", ""))

    def test_threshold_gate_rejects_low_score(self):
        # No numbers anywhere: the numeric gate is irrelevant and the
        # score alone must decide (probed partial_ratio_alignment ~36).
        self.assertFalse(
            _quote_matches("completely unrelated wording here", "alpha beta gamma delta")
        )

    def test_number_multiplicity_rejected(self):
        # Probed score 92.31 >= 92 with window nums {30}: the quote's
        # second '30' is absent from the window multiset -> reject.
        q = "session lasts 30 30 minutes"
        src = "the session lasts 30 minutes."
        self.assertGreaterEqual(
            __import__("rapidfuzz").fuzz.partial_ratio(q.lower(), src.lower()), 92
        )
        self.assertFalse(_quote_matches(q, src))

    def test_mutated_number_absent_from_window_rejected(self):
        # Unit-level twin of frozen C7 (score 96.55, window {30}, quote {300}).
        self.assertFalse(
            _quote_matches("The session lasts 300 minutes.", "The session lasts 30 minutes.")
        )

    def test_mutated_number_co_occurring_outside_window_rejected(self):
        # Implementation-review round 1 finding: the mutation's value DOES
        # occur in the chunk ("Page 7.") but OUTSIDE the aligned window —
        # whole-chunk CONTAINMENT (the wrong-scope gate in weakened form)
        # accepts this materially mutated fact; the window gate rejects it.
        # Probed score 96.00, window nums {30}, quote nums {7}.
        q = "The upgrade took 7 minutes to complete the rollout"
        src = "Page 7. The upgrade took 30 minutes to complete the rollout."
        self.assertGreaterEqual(
            __import__("rapidfuzz").fuzz.partial_ratio(q.lower(), src.lower()), 92
        )
        self.assertFalse(_quote_matches(q, src))

    def test_quote_at_end_of_long_numeric_chunk_accepted(self):
        # Discriminates the dest/src slicing bug (plan-critic round 1):
        # the quote's window is the FINAL sentence; a src-sliced window
        # (quote offsets applied to the source) would cover the numeric
        # prefix {42, 99} and reject. Probed score 98.31.
        chunk = (
            "Chapter 42 intro line. Section 99 overview. "
            "The session lasts 30 minutes."
        )
        self.assertTrue(_quote_matches("The session, lasts 30 minutes.", chunk))

    def test_numeric_containment_boundary_is_position_blind(self):
        # Documented residual boundary (approved plan, Disclosed residual):
        # containment ignores POSITIONAL correspondence — a quote number
        # that coincidentally occurs anywhere in the aligned window
        # satisfies the gate. End-to-end this is not constructable at
        # threshold >= 92 (probed reorder/co-occurrence shapes score 68-84
        # or collapse to the strict substring path), so the boundary is
        # pinned at the helper level where it is exact.
        self.assertTrue(_numeric_submultiset(["300"], ["30", "300"]))
        self.assertFalse(_numeric_submultiset(["300"], ["30"]))
        self.assertFalse(_numeric_submultiset(["30", "30"], ["30", "300"]))
        self.assertTrue(_numeric_submultiset([], ["30"]))

    def test_numeric_multiset_ordering(self):
        self.assertEqual(
            _numeric_multiset("30 30 and 2024"),
            ["2024", "30", "30"],
        )

    def test_verify_quote_whole_file_operand(self):
        # The PUT /wiki/claims active-transition re-verification
        # (wiki.py) passes the WHOLE parsed file as the source operand;
        # numbers elsewhere in the file must not veto a middle quote.
        # Probed score 100.0 for the middle-sentence window.
        parsed_text = (
            "Cover 3. Intro 88 units. "
            "The audit found 12 issues in 2024, per the report. "
            "Appendix 45. Index 9."
        )
        self.assertTrue(
            verify_quote("The audit found 12 issues in 2024, per the report.", parsed_text)
        )


class TestActiveIfVerifiedMatrix(unittest.TestCase):
    def setUp(self):
        self._snap = _snapshot_flags()

    def tearDown(self):
        _restore_flags(self._snap)

    def _curate(self, payload, chunks, raw=None):
        curator = WikiCurator(client=_StubCuratorClient(payload, raw=raw))
        return asyncio.run(
            curator.curate(vault_id=1, file_id=1, chunks=chunks)
        )

    def test_flag_off_unknown_chunk_kept_needs_review_own_id(self):
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = False
        out = self._curate(
            {"claims": [_claim("9_9", "The session lasts 30 minutes.")]},
            [CuratorChunk(chunk_id="1_0", source_text="The session lasts 30 minutes.", file_id=1)],
        )
        self.assertEqual(len(out.accepted), 1)
        a = out.accepted[0]
        self.assertEqual(a.status, "needs_review")
        self.assertEqual(a.chunk_id, "9_9")  # own cited id, never chunks[0]
        self.assertIsNone(a.file_id)  # no fabricated provenance

    def test_flag_off_empty_chunk_id_kept_needs_review(self):
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = False
        out = self._curate(
            {"claims": [_claim("", "The session lasts 30 minutes.")]},
            [CuratorChunk(chunk_id="1_0", source_text="The session lasts 30 minutes.", file_id=1)],
        )
        self.assertEqual(len(out.accepted), 1)
        a = out.accepted[0]
        self.assertEqual(a.status, "needs_review")
        self.assertEqual(a.chunk_id, "")
        self.assertIsNone(a.file_id)
        self.assertIsNone(a.source_label)

    def test_flag_off_known_chunk_quote_match_activates(self):
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = False
        out = self._curate(
            {"claims": [_claim("1_0", "The session lasts 30 minutes.")]},
            [CuratorChunk(chunk_id="1_0", source_text="The session lasts 30 minutes.", file_id=7,
                          source_label="file:7")],
        )
        self.assertEqual([(a.status, a.chunk_id, a.file_id) for a in out.accepted],
                         [("active", "1_0", 7)])

    def test_flag_on_unknown_chunk_rejected_with_lint(self):
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        # Lint findings only emit when a store is attached; a bare object
        # satisfies the not-None check the emitters use.
        curator = WikiCurator(client=_StubCuratorClient(
            {"claims": [_claim("9_9", "The session lasts 30 minutes.")]}
        ), store=object())
        out = asyncio.run(curator.curate(
            vault_id=1,
            file_id=1,
            chunks=[CuratorChunk(chunk_id="1_0", source_text="The session lasts 30 minutes.", file_id=1)],
        ))
        self.assertEqual(out.accepted, [])
        self.assertEqual([r.reason for r in out.rejected], ["missing_chunk_id"])
        self.assertEqual(len(out.lint_findings), 1)

    def test_draft_mode_always_needs_review(self):
        settings.wiki_llm_curator_mode = "draft"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        out = self._curate(
            {"claims": [_claim("1_0", "The session lasts 30 minutes.")]},
            [CuratorChunk(chunk_id="1_0", source_text="The session lasts 30 minutes.", file_id=1)],
        )
        self.assertEqual([a.status for a in out.accepted], ["needs_review"])


class TestPromptSchema(unittest.TestCase):
    def test_prompt_omits_unconsumed_page_fields(self):
        self.assertNotIn('"page_title"', _CURATOR_SYSTEM_PROMPT)
        self.assertNotIn('"page_type"', _CURATOR_SYSTEM_PROMPT)


class TestTruncationResidualPin(unittest.TestCase):
    def test_truncated_in_open_questions_still_json_parse_failed(self):
        # Disclosed residual (approved plan): a reply truncated inside a
        # post-claims section (here open_questions) leaves an unbalanced
        # '{', extract_json's '[' scan returns the claims LIST, and
        # curate() rejects the whole reply as json_parse_failed — the
        # complete claims array is NOT salvaged. Removing entities/
        # relations shrank the loss window; it did not eliminate it.
        settings.wiki_llm_curator_mode = "draft"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        raw = (
            '{"claims": [{"claim_text": "Alpha is a fact", "claim_type": "fact", '
            '"source_quote": "alpha beta", "chunk_id": "1_0", "confidence": 0.9}], '
            '"open_questions": [{"question": "unfin'
        )
        curator = WikiCurator(client=_StubCuratorClient(raw=raw))
        out = asyncio.run(
            curator.curate(
                vault_id=1,
                file_id=1,
                chunks=[CuratorChunk(chunk_id="1_0", source_text="alpha beta gamma", file_id=1)],
            )
        )
        self.assertEqual(out.accepted, [])
        self.assertIn("json_parse_failed", out.errors)


class _FailingTransportAsyncClient:
    """AsyncClient stand-in whose post always raises ConnectError."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *_a, **_k):
        raise wcc.httpx.ConnectError("connection refused (unit)")


class _StatusTransportAsyncClient:
    """AsyncClient stand-in whose post returns a fixed HTTP status."""

    status_code = 404

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **_k):
        request = wcc.httpx.Request("POST", url)
        return wcc.httpx.Response(
            self.status_code, request=request, content=b'{"error": "unit"}'
        )


def _client(url="http://10.10.10.10:1/v1"):
    return CuratorClient(base_url=url, model="m", timeout=5)


class TestSharedBreakerRegistry(unittest.TestCase):
    def setUp(self):
        wcc._reset_shared_breakers()
        self.addCleanup(wcc._reset_shared_breakers)

    def _propose(self, client):
        async def run():
            return await client.propose([{"role": "user", "content": "x"}])

        return asyncio.run(run())

    def test_registry_keyed_by_endpoint(self):
        with patch.object(wcc.httpx, "AsyncClient", _FailingTransportAsyncClient), \
             patch.object(wcc, "assert_curator_url_safe", lambda url: None):
            a = _client("http://10.10.10.10:1/v1")
            b = _client("http://10.10.10.11:2/v1")
            last_a = None
            # fail_max=5: the 5th failure itself raises ConnectError while
            # OPENING the breaker; the 6th call fast-fails.
            for _ in range(6):
                try:
                    self._propose(a)
                except Exception as e:
                    last_a = e
            self.assertEqual(type(last_a).__name__, "CircuitBreakerError")
            # Different endpoint: its own breaker is still CLOSED.
            try:
                self._propose(b)
            except Exception as e:
                self.assertEqual(type(e).__name__, "ConnectError")

    def test_non_outage_404_does_not_count(self):
        with patch.object(wcc.httpx, "AsyncClient", _StatusTransportAsyncClient), \
             patch.object(wcc, "assert_curator_url_safe", lambda url: None):
            c = _client()
            with self.assertRaises(wcc.httpx.HTTPStatusError):
                self._propose(c)
            self.assertEqual(c._breaker.fail_counter, 0)

    def test_outage_500_counts(self):
        _StatusTransportAsyncClient.status_code = 500
        self.addCleanup(setattr, _StatusTransportAsyncClient, "status_code", 404)
        with patch.object(wcc.httpx, "AsyncClient", _StatusTransportAsyncClient), \
             patch.object(wcc, "assert_curator_url_safe", lambda url: None):
            c = _client()
            with self.assertRaises(wcc.httpx.HTTPStatusError):
                self._propose(c)
            self.assertEqual(c._breaker.fail_counter, 1)

    def test_concurrent_event_loops_accumulate(self):
        # Direct-caller boundary pin (approved plan residual): two
        # CONCURRENT event loops through one registry entry. Production
        # paths serialize through the single-worker offload executor
        # (wiki_compiler._maybe_run_curator always dispatches there), so
        # this shape is only reachable by direct propose() callers. The
        # assertion pins the OBSERVED boundary — counts accumulate and
        # the named failure signature ('released too many times' /
        # RuntimeError from the breaker's internal lock) does not occur —
        # it does not claim general thread-safety.
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            with patch.object(wcc.httpx, "AsyncClient", _FailingTransportAsyncClient), \
                 patch.object(wcc, "assert_curator_url_safe", lambda url: None):
                c = _client("http://10.10.10.12:3/v1")

                def run():
                    async def call():
                        barrier.wait(timeout=10)
                        try:
                            await c.propose([{"role": "user", "content": "x"}])
                        except Exception as e:  # noqa: BLE001 - recorded
                            return e
                    return asyncio.run(call())

                errors.append(run())

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        self.assertFalse(t1.is_alive() and t2.is_alive())
        self.assertEqual(len(errors), 2)
        for e in errors:
            self.assertIsNotNone(e)
            self.assertNotIsInstance(e, RuntimeError)
            self.assertNotIn("released too many times", str(e))
        breaker = wcc._BREAKER_REGISTRY[("http://10.10.10.12:3/v1", "m")]
        self.assertEqual(breaker.fail_counter, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
