"""AC7 behavioral coverage for issue #712 (NOT a frozen check file).

The frozen C7 check is a structural wiring scan (a production file outside
retrieval_evaluator.py contains the token ``fail_open``). This suite pins the
SURFACE it named: the telemetry counter, its /metrics exposition, the
empty→retry→empty path (one fail-open, both no-think controls on the retry),
and the recorder's thread-safety contract.
"""

import asyncio
import os
import sys
import threading
import types
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)

if "lancedb.index" not in sys.modules:
    _ldb_idx = types.ModuleType("lancedb.index")
    _ldb_idx.FTS = type("FTS", (), {})
    _ldb_idx.IvfPq = type("IvfPq", (), {})
    sys.modules["lancedb.index"] = _ldb_idx

from app.services.retrieval_evaluator import RetrievalEvaluator
from app.services.telemetry import Telemetry, get_telemetry


class _ReasoningFakeClient:
    """Records every chat_completion's kwargs; models a thinking server that
    consumes the completion budget with reasoning unless the request disables
    it (mirrors the frozen C6 fake's semantics)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_completion(self, messages, max_tokens=None, temperature=None, **kw):
        self.calls.append(dict(kw))
        return self.responses.pop(0)


async def test_each_fail_open_reason_records_and_exposes_on_metrics(tmp_path):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with patch("app.services.telemetry._singleton", tel):
        # exception path
        class _Boom:
            async def chat_completion(self, *a, **k):
                raise RuntimeError("llm down")

        RetrievalEvaluator.fail_open_count = 0
        verdict = await RetrievalEvaluator(_Boom()).evaluate(
            "q", [{"text": "chunk"}]
        )
        assert verdict == "CONFIDENT"
        # unexpected-response path
        client = _ReasoningFakeClient(["I have no idea"])
        verdict = await RetrievalEvaluator(client).evaluate(
            "q", [{"text": "chunk"}]
        )
        assert verdict == "CONFIDENT"
        # empty-response path (empty → retry → empty)
        client2 = _ReasoningFakeClient(["", ""])
        verdict = await RetrievalEvaluator(client2).evaluate(
            "q", [{"text": "chunk"}]
        )
        assert verdict == "CONFIDENT"
    assert RetrievalEvaluator.fail_open_count == 3
    text = tel.metrics_text()
    assert 'ragapp_retrieval_evaluator_fail_open_total{reason="exception"} 1' in text
    assert 'ragapp_retrieval_evaluator_fail_open_total{reason="empty_response"} 1' in text
    assert (
        'ragapp_retrieval_evaluator_fail_open_total{reason="unexpected_response"} 1'
        in text
    )


async def test_empty_response_retries_once_with_union_of_no_think_controls():
    client = _ReasoningFakeClient(["", "NO_MATCH"])
    verdict = await RetrievalEvaluator(client).evaluate(
        "q", [{"text": "unrelated"}]
    )
    assert verdict == "NO_MATCH"
    assert len(client.calls) == 2
    first, retry = client.calls
    assert first.get("reasoning_effort") == "none"
    assert "chat_template_kwargs" not in first
    # The retry carries the UNION: reasoning_effort stays "none" (omitting it
    # would re-arm the client's default "high") plus the template control.
    assert retry.get("reasoning_effort") == "none"
    assert retry.get("chat_template_kwargs") == {"enable_thinking": False}


async def test_empty_response_fails_open_exactly_once():
    client = _ReasoningFakeClient(["", ""])
    RetrievalEvaluator.fail_open_count = 0
    verdict = await RetrievalEvaluator(client).evaluate(
        "q", [{"text": "chunk"}]
    )
    assert verdict == "CONFIDENT"
    assert RetrievalEvaluator.fail_open_count == 1
    assert len(client.calls) == 2, "one primary + one bounded retry"
    RetrievalEvaluator.fail_open_count = 0


def test_evaluator_recorders_reach_the_live_singleton():
    tel = Telemetry(enabled=True, registry_dir=None)
    with patch("app.services.telemetry._singleton", tel):
        tel.record_evaluator_fail_open("exception")
        assert tel._evaluator_fail_opens == {"exception": 1}
    tel2 = get_telemetry()
    assert isinstance(tel2, Telemetry)


def test_runtime_error_during_flush_cannot_escape_concurrent_recorders(tmp_path):
    """AC3's class at stress depth: many threads recording while json.dumps
    is poisoned must not raise out of any recorder call."""
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    raised = []

    import app.services.telemetry as telemetry_module

    with patch.object(
        telemetry_module.json, "dumps", side_effect=RuntimeError("dictionary changed size during iteration")
    ):
        def worker():
            try:
                for _ in range(50):
                    tel.record_embedding_cache(True)
                    tel.record_evaluator_fail_open("empty_response")
            except Exception as exc:  # noqa: BLE001
                raised.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert raised == [], f"recorder leaked an exception: {raised}"
    # Counters survived the poisoned flushes (mutations committed under lock).
    assert tel._embedding_cache_hits == 8 * 50
    assert tel._evaluator_fail_opens == {"empty_response": 8 * 50}


def test_record_evaluator_fail_open_validates_reason():
    tel = Telemetry(enabled=True, registry_dir=None)
    try:
        tel.record_evaluator_fail_open("not-a-reason")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown reason must raise ValueError")
    asyncio.run(_noop())


async def _noop():
    return None
