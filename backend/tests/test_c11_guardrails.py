"""Issue #716 guardrails (Phase 4.2) — source censuses + the behavioral case
the frozen C03 fixture cannot see.

Pins: (1) all FOUR wiki page-joining surfaces filter retired pages; (2) an
ACTIVE query-matching claim on an ARCHIVED page yields zero WikiEvidence;
(3) all five confidence reads are None-aware (stored 0.0 survives); (4) both
query-transform cache keys read PROMPT_VERSION at call time; (5) the
multimodal parser truncates retrieval_aids at MAX_AIDS.
"""

import os
import re
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import query_transformer as qt  # noqa: E402
from app.services import reranking, wiki_retrieval  # noqa: E402
from app.services.multimodal_prompts import (  # noqa: E402
    MAX_AIDS,
    parse_derived_response,
)
from app.services.wiki_retrieval import (  # noqa: E402
    _chain_conf_or,
    _conf_or,
)

WIKI_SRC = os.path.join(os.path.dirname(__file__), "..", "app", "services", "wiki_retrieval.py")
QT_SRC = os.path.join(os.path.dirname(__file__), "..", "app", "services", "query_transformer.py")
RETIRED = "NOT IN ('archived','stale')"


def _source(path: str) -> str:
    return open(path, encoding="utf-8").read()


def test_all_four_wiki_surfaces_filter_retired_pages():
    """The retired-page predicate must exist in every page-joining surface:
    _relation_lookup, _get_page_evidence, _fts_claim_search, _fts_page_search."""
    src = _source(WIKI_SRC)
    for fn in ("_relation_lookup", "_get_page_evidence", "_fts_claim_search", "_fts_page_search"):
        start = src.index(f"def {fn}")
        end = src.index("\n    def ", start)
        body = src[start:end]
        assert RETIRED in body, f"{fn} lost the retired-page filter"


async def test_active_claim_on_archived_page_not_served():
    """See docstring above — real migrated DB, archived page with an ACTIVE
    query-matching claim; retrieve() must return zero evidence for it.
    Reuses the frozen C3/C4 harness helpers (real DB + queue-backed pool)."""
    import shutil

    from test_c11_wiki_status_filter import (  # noqa: E402
        VAULT_ID,
        _make_seeded_service,
    )

    pages = [
        (9161, VAULT_ID, "retired-entity", "Retired Entity", "entity",
         "# Retired Entity", "Retired page summary.", "archived"),
    ]
    claim_text = "Retired Entity deployed the zenith array"
    claims = [
        (9162, VAULT_ID, 9161, claim_text, "Retired Entity", "deployed",
         "zenith array", "fact", "manual", "active", 0.9),
    ]
    service, pool, tmp = _make_seeded_service(pages, claims)
    try:
        results = service.retrieve("zenith array", vault_id=VAULT_ID)
    finally:
        pool.close_all()
        shutil.rmtree(tmp, ignore_errors=True)
    hits = [r for r in results if getattr(r, "page_id", None) == 9161]
    assert hits == [], (
        f"active claim on an archived page must not serve evidence: {hits}"
    )


def test_confidence_reads_are_none_aware():
    """All five confidence reads must be None-aware (stored 0.0 survives)."""
    src = _source(WIKI_SRC)
    assert 'd.get("confidence") or' not in src, (
        "raw `or`-default confidence read regressed (#716 T1-35-K-07)"
    )
    assert 'd.get("claim_confidence") or' not in src, (
        "raw `or`-default chain read regressed (#716 T1-35-K-07)"
    )


def test_cache_keys_read_prompt_version_at_call_time():
    """Both _make_cache_key bodies reference PROMPT_VERSION dynamically (no
    default-arg capture) so a version bump changes live keys."""
    src = _source(QT_SRC)
    assert 'PROMPT_VERSION = "' in src, "PROMPT_VERSION constant removed"
    assert src.count('"version": PROMPT_VERSION') == 2, (
        "both cache-key pre-images must carry the version at call time"
    )
    assert qt.PROMPT_VERSION


def test_reranker_span_uses_configured_model():
    """The span attribute must read the configured reranker, not a payload
    key (#716 T1-35-K-10 source pin)."""
    src = _source(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "reranking.py")
    )
    assert 'str(self.reranker_model)' in src
    assert 'payload.get("model"' not in src


def test_multimodal_aids_truncate_at_max():
    """The parser truncates to MAX_AIDS instead of rejecting (#716)."""
    import json

    payload = json.dumps(
        {
            "description": "A widget.",
            "retrieval_aids": [f"aid-{i}" for i in range(MAX_AIDS + 4)],
        }
    )
    result = parse_derived_response(payload, "v1")
    assert len(result["retrieval_aids"]) == MAX_AIDS


@pytest.mark.asyncio
async def test_rerank_negative_index_falls_back_through_public_rerank():
    """PRR-023/024 (feedback, reviewer-fixed): the consumption-layer backstop
    and the negative-index half of the producer check were untested. The
    service is constructed with a TRUTHY endpoint so rerank() dispatches
    _rerank_via_endpoint, which a patch forces to return the bad indices —
    the patched producer IS the executed path (reviewer probe: with the
    production backstop deleted this test goes RED)."""
    from unittest.mock import patch

    service = reranking.RerankingService(
        reranker_url="http://tei.test", reranker_model="bge-reranker-test"
    )
    chunks = [{"text": "a"}, {"text": "b"}]

    async def _bad_producer(query, texts, top_n):
        return [(-1, 0.9), (0, 0.5)][:top_n]

    with patch.object(
        service, "_rerank_via_endpoint", side_effect=_bad_producer
    ), patch.object(reranking, "reranking_cb") as _cb:
        _cb.return_value = lambda f: f
        out, success = await service.rerank(query="q", chunks=chunks, top_n=2)
    assert success is False
    assert out == chunks, "fallback must return the original chunks unmodified"


def test_confidence_none_branch_has_behavioral_pin():
    """PRR-017 (feedback): the C04 frozen seed sets confidence EXPLICITLY to
    0.0, which is indistinguishable at the storage layer from the schema
    DEFAULT 0.0 — pin the None branch (a genuinely NULL confidence falls to
    the default) so the None-aware mechanism is behaviorally covered."""

    assert _conf_or({"confidence": None}, 0.7) == 0.7
    assert _conf_or({"confidence": 0.0}, 0.7) == 0.0
    assert _conf_or({}, 0.6) == 0.6
    assert _chain_conf_or({"claim_confidence": 0.0}, "claim_confidence", 0.8) == 0.0


def test_planner_cache_key_is_premise_sensitive():
    """PRR-025 (feedback): the query-plan cache key's version component is
    pinned behaviorally (not just by the source census) — changing
    PROMPT_VERSION changes QueryPlanner._make_cache_key."""
    from app.services import query_transformer as _qt

    planner = _qt.QueryPlanner.__new__(_qt.QueryPlanner)
    planner.__dict__["_llm_client"] = type("M", (), {"model": "m"})()
    base_key = _qt.QueryPlanner._make_cache_key(planner, "q")
    with patch.object(_qt, "PROMPT_VERSION", _qt.PROMPT_VERSION + "-next"):
        other_key = _qt.QueryPlanner._make_cache_key(planner, "q")
    assert base_key != other_key



@pytest.mark.asyncio
async def test_match_containment_whitespace_tolerant():
    """PRR-004 feedback-regression pin (reviewer F2): the distiller rebuilds
    chunk text with normalized single-space joins while the parent window is
    a raw document slice — the whitespace-tolerant elif must wrap the
    PARENT'S own span in-place instead of falling to the duplicate-appending
    fallback (RED at the pre-fix feedback head: the fallback fired and the
    content appeared twice)."""
    from unittest.mock import patch as _patch

    from app.services.document_retrieval import RAGSource
    from app.services.prompt_builder import PromptBuilderService

    pb = PromptBuilderService()
    src = RAGSource(
        text="Core facts. Revenue grew across EMEA.",
        file_id="42",
        score=0.85,
        metadata={
            "source_file": "test.pdf",
            "raw_text": "Core facts. REMOVED SENTINEL. Revenue grew across EMEA.",
        },
    )
    src.parent_window_text = (
        "Core facts.\tRevenue grew across EMEA.\nMore context here."
    )

    with _patch("app.services.prompt_builder.settings") as mock_settings:
        mock_settings.parent_retrieval_enabled = True
        out = pb.format_chunk(src, 1)

    assert "[[MATCH: Core facts." in out, (
        "whitespace-tolerant containment must wrap the parent's own span "
        f"in-place; got: {out!r}"
    )
    body = out.split("<document>", 1)[-1]
    assert out.count("Revenue grew across EMEA.") == 1, (
        f"content duplicated — fallback fired instead of in-place wrap: {out!r}"
    )
    assert "REMOVED SENTINEL" not in out, (
        "suppressed sentence re-injected (AC1 invariant)"
    )
