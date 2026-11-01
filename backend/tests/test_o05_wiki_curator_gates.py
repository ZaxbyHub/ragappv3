"""RED tests for issue #795: wiki curator quote-gating and breaker gaps.

Six acceptance criteria, each authored to fail at the pre-fix HEAD and to
pass once the corresponding fix lands:

  1. _quote_matches fuzzy path compares the quote's numeric multiset
     against the WHOLE chunk, so digits outside the matched window
     (e.g. a "Page 7. " header) veto an otherwise legitimate quote.
  2. _CURATOR_SYSTEM_PROMPT still requests "entities" and "relations"
     sections that WikiCurator never consumes (it only reads claims /
     contradictions / open_questions), paying output tokens for data
     that is parsed and dropped.
  3. mode=active_if_verified stamps status='active' even when
     require_quote_match=False, i.e. for a claim whose source_quote was
     never verified at all.
  4. When require_chunk_id=False and the LLM cites an unknown chunk_id,
     verification silently falls back to chunks[0], fabricating
     provenance (accepted claim carries chunks[0].chunk_id).
  5. WikiCompiler._build_curator_chunks bounds the curator input by
     max(_COMPILE_CHUNK_SIZE, wiki_llm_curator_max_input_chars) = 6000
     chars, but the setting's documentation in app/config.py promises
     the bound is applied "per call" -- a 20000-char document is only
     30% covered, silently.
  6. Each CuratorClient builds its OWN AsyncCircuitBreaker (fail_max=5),
     so failures never accumulate across client instances: an endpoint
     that refuses every connection keeps raising raw ConnectError
     forever instead of tripping a shared breaker.

These tests are assertion-identical to the ACs; they MUST FAIL at HEAD.
Tests are module-level functions (no TestCase wrapper) so the frozen
check argvs can address each by its bare node id.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (mirrors tests/test_wiki_curator.py).
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
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

import httpx

import app.services.wiki_curator_client as wcc
from app.config import settings
from app.services.wiki_curator import (
    _CURATOR_SYSTEM_PROMPT,
    CuratorChunk,
    WikiCurator,
    _quote_matches,
)
from app.services.wiki_curator_client import CuratorClient

# Settings fields these tests touch; snapshot before, restore after (in
# each test's finally), so no curator flag state leaks between tests (or
# into other suites).
_CURATOR_FLAG_FIELDS = (
    "wiki_llm_curator_mode",
    "wiki_llm_curator_require_quote_match",
    "wiki_llm_curator_require_chunk_id",
)


def _snapshot_curator_flags() -> dict:
    return {f: getattr(settings, f) for f in _CURATOR_FLAG_FIELDS}


def _restore_curator_flags(snap: dict) -> None:
    for f, v in snap.items():
        setattr(settings, f, v)


class _StubCuratorClient:
    """Minimal CuratorClient stand-in: propose() returns the canned
    json.dumps(payload) for the per-test claim payload. The verification
    logic under test runs after extract_json parses this string."""

    def __init__(self, payload: dict):
        self.payload = json.dumps(payload)
        self.calls = 0

    async def propose(self, *_a, **_k):
        self.calls += 1
        return self.payload


# ---------------------------------------------------------------------------
# AC1: fuzzy-path numeric gate must scope to the matched window
# ---------------------------------------------------------------------------


def test_quote_match_ignores_numbers_outside_matched_window():
    """Issue #795: a number outside the matched window (a page header)
    must not veto an otherwise verbatim-equivalent quote. At HEAD the
    fuzzy path compares the quote's numeric multiset against the WHOLE
    chunk, so the stray '7' in 'Page 7. ' forces a False."""
    q = "The team completed 12 audits during 2024 per the annual report"
    src = "Page 7. The team completed 12 audits in 2024, per the annual report."
    # Sanity: without the 'Page 7. ' prefix the quote verifies.
    assert _quote_matches(
        q, "The team completed 12 audits in 2024, per the annual report."
    ) is True
    assert _quote_matches(q, src) is True


# ---------------------------------------------------------------------------
# AC2: prompt schema must not request unconsumed sections
# ---------------------------------------------------------------------------


def test_prompt_schema_requests_only_consumed_sections():
    """Issue #795: WikiCurator.curate only consumes 'claims',
    'contradictions', and 'open_questions'. The system prompt still
    requests 'entities' and 'relations' arrays that are parsed and
    dropped -- wasted output tokens and an invitation to hallucinate
    structure nobody reads."""
    assert [k for k in ("entities", "relations") if f'"{k}"' in _CURATOR_SYSTEM_PROMPT] == []


# ---------------------------------------------------------------------------
# AC3: active_if_verified must not activate an unverified quote
# ---------------------------------------------------------------------------


def test_active_if_verified_unverified_quote_is_needs_review():
    """Issue #795: 'active_if_verified' means active ONLY when the quote
    was actually verified. With require_quote_match=False no
    verification happens at all, so the claim must land as
    needs_review -- at HEAD it lands as active."""
    snap = _snapshot_curator_flags()
    try:
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = False
        settings.wiki_llm_curator_require_chunk_id = True

        payload = {
            "claims": [
                {
                    "claim_text": "Alpha is verified",
                    "claim_type": "fact",
                    "source_quote": (
                        "Sentence that appears nowhere in the chunk."
                    ),
                    "chunk_id": "1_0",
                    "confidence": 0.9,
                }
            ]
        }
        chunks = [
            CuratorChunk(
                chunk_id="1_0",
                source_text="Alpha beta gamma delta epsilon.",
                file_id=1,
            )
        ]
        cur = WikiCurator(client=_StubCuratorClient(payload))
        out = asyncio.run(cur.curate(vault_id=1, file_id=1, chunks=chunks))
        assert [a.status for a in out.accepted] == ['needs_review']
    finally:
        _restore_curator_flags(snap)


# ---------------------------------------------------------------------------
# AC4: unknown chunk_id must not fabricate chunks[0] provenance
# ---------------------------------------------------------------------------


def test_unknown_chunk_id_never_falls_back_to_first_chunk():
    """Issue #795: when require_chunk_id=False and the LLM cites an
    unknown chunk_id, verification falls back to chunks[0] and the
    accepted claim inherits chunks[0]'s chunk_id/file_id -- fabricated
    provenance for a quote the model attributed to a nonexistent
    chunk."""
    snap = _snapshot_curator_flags()
    try:
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = False

        payload = {
            "claims": [
                {
                    "claim_text": "The team completed 12 audits in 2024",
                    "claim_type": "fact",
                    "source_quote": "The team completed 12 audits in 2024",
                    "chunk_id": "9_9",
                    "confidence": 0.9,
                }
            ]
        }
        chunks = [
            CuratorChunk(
                chunk_id="1_0",
                source_text=(
                    "The team completed 12 audits in 2024, per the annual "
                    "report."
                ),
                file_id=1,
            ),
            CuratorChunk(
                chunk_id="1_1",
                source_text="unrelated second chunk",
                file_id=1,
            ),
        ]
        cur = WikiCurator(client=_StubCuratorClient(payload))
        out = asyncio.run(cur.curate(vault_id=1, file_id=1, chunks=chunks))
        got = (
            any(a.status == "active" for a in out.accepted),
            any(a.chunk_id == "1_0" for a in out.accepted),
        )
        assert got == (False, False)
    finally:
        _restore_curator_flags(snap)


# ---------------------------------------------------------------------------
# AC5: the curator input bound must match its documented semantics
# ---------------------------------------------------------------------------


def test_curator_input_bound_matches_its_documentation():
    """Issue #795: app/config.py documents
    wiki_llm_curator_max_input_chars as the maximum source text passed
    to the curator 'per call', but WikiCompiler._build_curator_chunks
    applies it ONCE per document (budget = max(2000, 6000)), so a
    20000-char document is silently only 30% curated. Either the bound
    must paginate per call or the documentation must stop saying 'per
    call'."""
    from app.services.wiki_compiler import WikiCompiler

    t = "x" * 20000
    chunks = WikiCompiler._build_curator_chunks(text=t, file_id=1)
    covered = sum(len(c.source_text) for c in chunks)

    # The docline is the line immediately after the
    # wiki_llm_curator_max_input_chars assignment in app/config.py.
    config_path = Path(__file__).resolve().parents[1] / "app" / "config.py"
    docline = ""
    with open(config_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if "wiki_llm_curator_max_input_chars" in line and "=" in line:
            docline = lines[i + 1]
            break

    ok = covered == len(t) or "per call" not in docline
    assert ok is True


# ---------------------------------------------------------------------------
# AC6: curator failures must accumulate across client instances
# ---------------------------------------------------------------------------


def test_curator_breaker_accumulates_failures_across_clients():
    """Issue #795: each CuratorClient constructs its own
    AsyncCircuitBreaker (fail_max=5), so a persistently unreachable
    endpoint never trips anything -- every call surfaces a raw
    ConnectError. Failures must accumulate ACROSS clients (shared
    breaker scope) so repeated outages fail fast with
    CircuitBreakerError."""
    try:
        class _ConnectErrorAsyncClient:
            """httpx.AsyncClient stand-in whose every post() raises
            httpx.ConnectError -- models an endpoint refusing every
            connection without any real network I/O."""

            def __init__(self, *_a, **_k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, *_a, **_k):
                raise httpx.ConnectError("connection refused (probe)")

        async def runner():
            last_exc = None
            # Four fresh clients; 3 + 3 + 3 + 1 = 10 total failed calls.
            # No single client reaches its per-instance fail_max=5, so a
            # per-client breaker can never open here.
            for counts in (3, 3, 3, 1):
                client = CuratorClient(
                    base_url="http://10.10.10.10:1/v1", model="m"
                )
                for _ in range(counts):
                    try:
                        await client.propose(
                            [{"role": "user", "content": "hi"}]
                        )
                    except Exception as e:
                        last_exc = e
            return last_exc

        with patch.object(
            wcc, "assert_curator_url_safe", lambda *_a, **_k: None
        ), patch.object(
            wcc.httpx, "AsyncClient", _ConnectErrorAsyncClient
        ):
            last_exc = asyncio.run(runner())
        assert type(last_exc).__name__ == 'CircuitBreakerError'
    finally:
        # Defensive: a future fix may introduce a module-level shared
        # breaker registry in wiki_curator_client. Clear it so no
        # breaker state leaks between tests. No-op at HEAD (the
        # attribute does not exist yet).
        reg = getattr(wcc, "_BREAKER_REGISTRY", None)
        reg.clear() if isinstance(reg, dict) else None


if __name__ == "__main__":
    unittest.main()
