"""AC7 behavioral coverage for issue #712 (NOT a frozen check file).

The frozen C7 check is a structural wiring scan (a production file outside
retrieval_evaluator.py contains the token ``fail_open``). This suite pins the
SURFACE it named: the telemetry counter, its /metrics exposition, the
empty→retry→empty path (one fail-open, both no-think controls on the retry),
and the recorder's thread-safety contract.
"""

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
    """Records every chat_completion call (kwargs AND max_tokens); models a
    thinking server that consumes the completion budget with reasoning unless
    the request disables it (mirrors the frozen C6 fake's semantics)."""

    def __init__(self, responses, model="some-reasoning-model", reasoning_effort="high"):
        self.responses = list(responses)
        self.calls = []
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def chat_completion(self, messages, max_tokens=None, temperature=None, **kw):
        self.calls.append({**kw, "_max_tokens": max_tokens})
        return self.responses.pop(0)


async def test_each_fail_open_reason_records_and_exposes_on_metrics(tmp_path):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    prior = RetrievalEvaluator.fail_open_count
    try:
        await _exercise_all_fail_open_reasons(tel)
    finally:
        RetrievalEvaluator.fail_open_count = prior


async def _exercise_all_fail_open_reasons(tel):
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


async def test_empty_response_retries_once_with_widened_budget_and_gated_control():
    """The bounded retry (issue #712): re-assert reasoning_effort (omitting
    it would re-arm the client's "high"), widen the budget so a server that
    reasons anyway can finish, and add the template-level control ONLY for a
    family that documents it (the C12 family gate)."""
    qwen = _ReasoningFakeClient(["", "NO_MATCH"], model="qwen/qwen3.5")
    verdict = await RetrievalEvaluator(qwen).evaluate(
        "q", [{"text": "unrelated"}]
    )
    assert verdict == "NO_MATCH"
    assert len(qwen.calls) == 2
    first, retry = qwen.calls
    assert first.get("reasoning_effort") == "none"
    assert "chat_template_kwargs" not in first, "the primary call sends one control"
    assert first["_max_tokens"] == 64
    assert retry.get("reasoning_effort") == "none"
    assert retry["_max_tokens"] > first["_max_tokens"], "the retry widens the budget"
    assert retry.get("chat_template_kwargs") == {"enable_thinking": False}

    # A family with no documented template control is never handed one.
    other = _ReasoningFakeClient(["", "NO_MATCH"], model="llama3.1")
    assert await RetrievalEvaluator(other).evaluate(
        "q", [{"text": "unrelated"}]
    ) == "NO_MATCH"
    assert "chat_template_kwargs" not in other.calls[1], (
        "an undocumented per-family control must not be hard-coded "
        f"({other.calls[1]})"
    )
    assert other.calls[1]["_max_tokens"] > other.calls[0]["_max_tokens"]


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

    # Patch telemetry's module-local alias, NOT the stdlib json module:
    # telemetry does `import json`, so patching json.dumps would mutate it
    # process-wide for the duration (#931 review PRR-022).
    with patch.object(
        telemetry_module, "_dumps", side_effect=RuntimeError("dictionary changed size during iteration")
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


def test_recorders_hold_the_lock_for_their_critical_section(tmp_path):
    """PRR-006 / C34: the concurrency guard must be FALSIFIABLE. The 8-thread
    stress below proves the flush never raises, but it stayed green with the
    lock replaced by a no-op — because under the GIL the counters rarely lose
    an update. This test asserts the invariant directly: while a recorder is
    mid-flush, another thread must NOT be able to acquire the lock. A no-op
    lock makes this fail, so removing the guard cannot pass silently.
    """
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def _blocking_flush():
        entered.set()
        release.wait(timeout=5)

    with patch.object(tel, "_flush_shard", side_effect=_blocking_flush):
        worker = threading.Thread(target=tel.record_embedding_cache, args=(True,))
        worker.start()
        try:
            assert entered.wait(timeout=5), "the recorder never reached the flush"
            acquired = []
            probe_error = []

            def _probe():
                try:
                    if tel._lock.acquire(blocking=False):
                        acquired.append(True)
                        tel._lock.release()
                except Exception as exc:  # noqa: BLE001 - surfaced below
                    probe_error.append(repr(exc))

            prober = threading.Thread(target=_probe)
            prober.start()
            prober.join(timeout=5)
            assert probe_error == [], (
                f"the lock probe itself failed, so this test would pass "
                f"vacuously: {probe_error}"
            )
            assert acquired == [], (
                "a recorder must hold Telemetry._lock across its critical "
                "section; another thread acquired it mid-flush, so the lock is "
                "not doing the work the concurrency guard claims"
            )
        finally:
            release.set()
            worker.join(timeout=5)
