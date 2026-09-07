"""Vision trace dedup counter tests (issue #462 B3 / OBS-003).

Pins the producer -> wire chain required by the issue's trace-accuracy
obligation: ``VisionEvidenceService.run()`` must report
``deduped = eligible - unique-selected`` computed BEFORE the independent cap,
the counter must survive the feature-off early return (counting is pure),
``RAGTrace`` must emit ``vision_deduped``, and the rag_engine mapping that
feeds the trace must stay wired.
"""

import asyncio
import inspect
import os
import sys
import types
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from app.services import rag_engine as rag_engine_module
from app.services.rag_trace import RAGTrace
from app.services.vision_evidence import VisionEvidenceService


@dataclass
class _Src:
    artifact_id: object
    modality: object = "image"
    asset_id: object = "a1"
    text: str = "proxy"


class _FakeClient:
    async def start(self):
        return None

    async def chat_multimodal(self, messages, max_tokens=512):
        return "observation"

    async def close(self):
        return None


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _run(sources, cap):
    svc = VisionEvidenceService()
    with (
        patch.object(settings, "multimodal_query_vision_enabled", True),
        patch.object(settings, "multimodal_max_assets_per_batch", cap),
        patch("app.services.vision_evidence._conn_ctx", lambda: _Ctx(MagicMock())),
        patch.object(
            VisionEvidenceService, "_whole_batch_allowed", lambda self, c, v: None
        ),
        patch.object(svc, "_client_factory", lambda *a, **k: _FakeClient()),
    ):
        return asyncio.run(svc.run(query="q", sources=sources, vault_id=1))


def test_run_counters_dup_scenario():
    """3 eligible sources with one duplicate and cap 1 -> eligible=3,
    selected=2, deduped=1, capped=1 (the exact OBS-003 numbers)."""
    result = _run(
        [
            _Src("art1", asset_id="x"),
            _Src("art1", asset_id="x"),
            _Src("art2", asset_id="y"),
        ],
        cap=1,
    )
    assert (result.eligible, result.selected, result.deduped, result.capped) == (
        3,
        2,
        1,
        1,
    )


def test_run_counters_unique_control():
    """Unique-only control: no duplicates -> deduped stays 0."""
    result = _run([_Src("art1"), _Src("art2")], cap=5)
    assert result.eligible == 2
    assert result.selected == 2
    assert result.deduped == 0
    assert result.capped == 0


def test_feature_off_still_reports_counts():
    """OBS-003 placement pin: counting happens BEFORE the feature-off early
    return, so the trace counters stay truthful with vision disabled (V5:
    statuses stay unset)."""
    svc = VisionEvidenceService()
    sources = [
        _Src("art1", asset_id="x"),
        _Src("art1", asset_id="x"),
        _Src("art2", asset_id="y"),
    ]
    with (
        patch.object(settings, "multimodal_query_vision_enabled", False),
        patch("app.services.vision_evidence._conn_ctx", lambda: _Ctx(MagicMock())),
    ):
        result = asyncio.run(svc.run(query="q", sources=sources, vault_id=1))
    assert (result.eligible, result.selected, result.deduped) == (3, 2, 1)
    assert result.statuses == {}
    assert result.vlm_used == 0


def test_trace_to_dict_emits_vision_deduped():
    """Wire emission: RAGTrace.to_dict() carries vision_deduped (safe default 0,
    producer value 1 under the duplicate scenario)."""
    assert RAGTrace().to_dict()["vision_deduped"] == 0
    result = _run(
        [
            _Src("art1", asset_id="x"),
            _Src("art1", asset_id="x"),
            _Src("art2", asset_id="y"),
        ],
        cap=1,
    )
    trace = RAGTrace()
    trace.vision_deduped = result.deduped
    assert trace.to_dict()["vision_deduped"] == 1


def test_rag_engine_mapping_line_wired():
    """Source-inspection pin (precedent:
    test_rag_engine_agentic_early_return.py::TestAgenticRAGCommentDocumentation):
    the engine must keep mapping the producer counter into the trace, or the
    OBS-003 fix is silently unwired on the runtime path."""
    src = inspect.getsource(rag_engine_module)
    assert "trace.vision_deduped = vision_result.deduped" in src
