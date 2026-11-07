"""Follow-up #1073 truncation-synonym contracts (issue #1076 AC4).

Both truncation guards compare ``finish_reason == "length"`` literally:

* the chunk-enrichment guard in
  ``ChunkEnrichmentService._generate_enrichment``
  (chunk_enrichment.py ~273) reads the per-call ``metrics_sink`` mirror;
* the canvas edit-range guard (canvas.py ~638-647) reads the shared
  client's ``last_metrics`` slot.

OpenAI-compatible providers report the output-token cap under the
completion-reason synonyms ``max_tokens`` and ``max_length`` as well; those
completions are silently accepted today — at the enrichment site a cut
curator response is stored as if complete, at the canvas site a truncated
``model_edit`` is spliced into the artifact. The contract: every token in
{length, max_tokens, max_length} is treated as truncation at BOTH sites;
``stop``, a NULL finish_reason, and an omitted finish_reason key remain
accepted.

The enrichment legs drive the REAL ``_generate_enrichment`` with the
probe's fake-client pattern (writes ``finish_reason`` into the
``metrics_sink`` kwarg, returns a well-formed JSON object). The canvas legs
reuse the issue-#688 harness verbatim (a minimal FastAPI app + TestClient
posting to the real ``/canvas/artifacts/{uid}/edit-range`` route, with the
canvas store patched and the LLM client a MagicMock whose ``last_metrics``
is a real dict) — an app-level TestClient, not a unit call of the handler,
because the guard reads ``request.app.state.llm_client`` and the route's
ordering (model call -> guard -> splice) is part of the contract.

RED at base (the max_tokens / max_length legs at both sites), GREEN after
the fix.
"""

import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# This module drives the canvas edit-range route the way the #688 harness
# does (no CSRF token on the request); the autouse conftest bypass applies.
CSRF_TEST_POLICY = "naive"

try:  # pragma: no cover - environments without lancedb
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.services.chunk_enrichment import ChunkEnrichmentService

# ---------------------------------------------------------------------------
# Enrichment site: the REAL ChunkEnrichmentService._generate_enrichment
# ---------------------------------------------------------------------------

_GOOD_REPLY = (
    '{"summary": "a faithful summary", '
    '"questions": ["what does this chunk cover?"], '
    '"entities": ["LanceDB"]}'
)

# Sentinel: the client reports NO finish_reason key at all in the sink.
_OMITTED = object()


class _FakeClient:
    """chat_completion double mirroring the metrics_sink contract.

    Writes the scripted ``finish_reason`` into the per-call ``metrics_sink``
    kwarg (the way the real LLMClient does, chunk_enrichment.py ~245-262)
    and returns a well-formed JSON summary/questions/entities object — so
    the only way the result can carry an ``error`` is the truncation guard.
    """

    def __init__(self, finish_reason):
        self._finish_reason = finish_reason

    async def chat_completion(self, messages, **kwargs):
        if self._finish_reason is not _OMITTED:
            sink = kwargs.get("metrics_sink")
            if sink is not None:
                sink["finish_reason"] = self._finish_reason
        return _GOOD_REPLY


def _mentions_truncation(error: str, token: str) -> bool:
    """Accept today's wording and the obvious fix wordings."""
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in ("truncat", "cut", "cap", "finish_reason", token)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "max_tokens", "max_length"])
async def test_enrichment_site_rejects_truncation_synonyms(finish_reason):
    """A completion reporting a truncation synonym is a failure, not a store.

    The fake client returns a CLOSED, well-formed JSON object whose
    ``finish_reason`` is the parametrized synonym, so the response parses
    cleanly — the guard is the only line that can reject it. RED at base
    (the max_tokens / max_length legs yield error=None), GREEN after the
    fix.

    Post-fix mutation that turns this RED: shrinking the synonym set back to
    the literal ``== "length"`` comparison (chunk_enrichment.py ~273).
    """
    service = ChunkEnrichmentService(_FakeClient(finish_reason))
    enrichment = await service._generate_enrichment(
        "c1", "some real chunk text", "Title", "Section"
    )
    assert enrichment.error, (
        "truncation synonym not recognized at the enrichment guard: "
        f"finish_reason={finish_reason!r} (a closed, well-formed reply) "
        f"produced error={enrichment.error!r}, summary="
        f"{enrichment.summary!r} — every token in "
        "{length, max_tokens, max_length} must be treated as truncation"
    )
    assert _mentions_truncation(enrichment.error, finish_reason), (
        "truncation synonym not recognized at the enrichment guard: the "
        f"error for finish_reason={finish_reason!r} does not mention the "
        f"truncation or the token: {enrichment.error!r}"
    )
    # A rejected generation carries no content fields.
    assert enrichment.summary == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason",
    ["stop", None, _OMITTED],
    ids=["stop", "null-finish-reason", "omitted-finish-reason"],
)
async def test_enrichment_site_accepts_non_truncation_finish_reasons(
    finish_reason,
):
    """stop / NULL / omitted finish_reasons stay accepted.

    Pins the boundary so the synonym expansion cannot over-block: a clean
    completion parses and populates the summary. GREEN at base and after
    the fix.
    """
    service = ChunkEnrichmentService(_FakeClient(finish_reason))
    enrichment = await service._generate_enrichment(
        "c1", "some real chunk text", "Title", "Section"
    )
    assert not enrichment.error, (
        "finish_reason="
        f"{finish_reason if finish_reason is not _OMITTED else '<omitted>'!r} "
        f"must stay accepted at the enrichment guard, got "
        f"error={enrichment.error!r}"
    )
    assert enrichment.summary == "a faithful summary"


# ---------------------------------------------------------------------------
# Canvas site: the REAL edit-range route (issue #688 TestClient harness)
# ---------------------------------------------------------------------------

_BASE_CONTENT = "alpha\nbeta\ngamma"

_VERSION_ROW = {
    "id": 55,
    "version_no": 2,
    "name": None,
    "origin": "model_edit",
    "content": "alpha\nrewritten\ngamma",
    "content_sha256": "sha-edit",
    "model_edit_json": None,
    "created_by": 1,
    "created_at": "2026-01-01T00:00:00Z",
}


def _minimal_canvas_app():
    """The issue-#688 minimal app: real router, overridden auth/db deps."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_active_user, get_db, get_vector_store
    from app.api.routes import canvas as canvas_module

    ready_vs = MagicMock()
    ready_vs._ready = True
    fake_conn = MagicMock()
    fake_conn.in_transaction = False

    app = FastAPI()
    app.include_router(canvas_module.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1,
        "username": "u",
        "email": "u@e",
        "role": "admin",
    }
    app.dependency_overrides[get_vector_store] = lambda: ready_vs
    app.dependency_overrides[get_db] = lambda: fake_conn
    return app, TestClient(app)


def _post_edit_range(client):
    return client.post(
        "/api/canvas/artifacts/uid/edit-range",
        json={
            "start_line": 2,
            "end_line": 2,
            "instruction": "rewrite",
            "base_version_no": 1,
        },
    )


def _canvas_app_with_client(finish_reason):
    """App + store where the model reply reports the given finish_reason.

    ``last_metrics`` is pinned to a REAL dict (the #688 empty-reply test's
    discipline): a bare MagicMock attribute would make the guard's
    ``.get("finish_reason")`` return a Mock and pass "by accident".
    """
    app, client = _minimal_canvas_app()
    store = MagicMock()
    store.get_by_uid.return_value = {
        "id": 9001,
        "artifact_uid": "uid",
        "session_id": 1,
        "message_id": None,
        "turn_id": None,
        "kind": "text",
        "name": "a.py",
        "language": "python",
        "vault_id": 1,
        "current_version_no": 1,
        "source_refs_json": None,
        "created_by": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    store.get_version.return_value = {"content": _BASE_CONTENT}
    store.append_version.return_value = dict(_VERSION_ROW)
    llm = MagicMock()
    if finish_reason is _OMITTED:
        llm.last_metrics = {}
    else:
        llm.last_metrics = {"finish_reason": finish_reason}
    llm.chat_completion = AsyncMock(return_value="rewritten")
    app.state.llm_client = llm
    return client, store


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens", "max_length"])
def test_canvas_edit_range_rejects_truncation_synonyms(finish_reason):
    """A synonym-truncated edit-range reply 502s, never records a version.

    The route reads the shared client's ``last_metrics`` after the model
    call (canvas.py ~638-647); with ``finish_reason`` set to the
    parametrized synonym the reply MUST be rejected with 502
    ``canvas_model_truncated`` and ``append_version`` MUST NOT run —
    splicing the fragment would record a truncated model_edit version.

    RED at base (the max_tokens / max_length legs fall through to a 200 and
    a recorded version), GREEN after the fix.

    Post-fix mutation that turns this RED: shrinking the canvas guard's
    token set back to the literal ``finish_reason == "length"`` comparison.
    """
    client, store = _canvas_app_with_client(finish_reason)
    with patch("app.api.routes.canvas.get_canvas_store", lambda: store):
        response = _post_edit_range(client)

    assert (
        response.status_code == 502
        and response.json().get("detail") == "canvas_model_truncated"
    ), (
        "truncation synonym not recognized at the canvas edit-range guard: "
        f"finish_reason={finish_reason!r} produced HTTP "
        f"{response.status_code} / detail={response.json().get('detail')!r} "
        f"(body={response.json()}), expected 502 canvas_model_truncated"
    )
    store.append_version.assert_not_called()


@pytest.mark.parametrize(
    "finish_reason",
    ["stop", None, _OMITTED],
    ids=["stop", "null-finish-reason", "omitted-finish-reason"],
)
def test_canvas_edit_range_accepts_non_truncation_finish_reasons(finish_reason):
    """stop / NULL / omitted finish_reasons: the edit applies (2xx).

    Pins the boundary so the synonym expansion cannot over-block: the
    replacement is spliced and a version recorded. GREEN at base and after
    the fix.
    """
    client, store = _canvas_app_with_client(finish_reason)
    with patch("app.api.routes.canvas.get_canvas_store", lambda: store):
        response = _post_edit_range(client)

    assert 200 <= response.status_code < 300, (
        "finish_reason="
        f"{finish_reason if finish_reason is not _OMITTED else '<omitted>'!r} "
        f"must stay accepted at the canvas guard, got HTTP "
        f"{response.status_code} / body={response.json()}"
    )
    store.append_version.assert_called_once()
