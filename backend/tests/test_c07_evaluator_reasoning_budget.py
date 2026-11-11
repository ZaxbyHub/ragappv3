"""Frozen checks for issue #712 (C07) — retrieval evaluator reasoning budget.

AC6: the retrieval evaluator must reach a real NO_MATCH verdict when the
backing server spends tokens on reasoning. A reasoning server that burns
1000 tokens thinking answers an un-budgeted ``max_tokens=64`` call with an
empty string; the evaluator currently treats that as a failure and fails
open to CONFIDENT, silently disabling CRAG correction.

AC7: the fail-open signal (``fail_open`` counter / readers) must have a
production reader outside the evaluator module itself, otherwise a
sustained evaluator outage stays invisible to operators.
"""

import os
import sys
import types
from pathlib import Path

# Stub optional heavy dependencies not installed in the test env.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

for _mod in ('lancedb', 'pyarrow'):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            _stub = types.ModuleType(_mod)
            sys.modules[_mod] = _stub

# lancedb.index must exist before vector_store.py is imported
if 'lancedb.index' not in sys.modules:
    _ldb_idx = types.ModuleType('lancedb.index')
    _ldb_idx.FTS = type('FTS', (), {})
    _ldb_idx.IvfPq = type('IvfPq', (), {})
    sys.modules['lancedb.index'] = _ldb_idx

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    for _sub in ('partition', 'partition.auto', 'chunking', 'chunking.title',
                 'documents', 'documents.elements'):
        sys.modules.setdefault(f'unstructured.{_sub}', types.ModuleType(f'unstructured.{_sub}'))
    sys.modules.setdefault('unstructured', types.ModuleType('unstructured'))

from app.services.retrieval_evaluator import RetrievalEvaluator

# The reasoning server burns this many tokens on thinking before any content.
_REASONING_TOKENS = 1000


class _ReasoningFakeClient:
    """LLM client double that models a reasoning server.

    Reasoning is ON unless the caller disables it via ``reasoning_effort``
    in {"none", "minimal"} or ``chat_template_kwargs`` containing
    ``{"enable_thinking": False}``. With reasoning on, a ``max_tokens``
    budget below reasoning+8 (i.e. below 1008) is consumed entirely by
    thinking, so the response body is empty; otherwise the server answers
    with the classification verdict "NO_MATCH".
    """

    async def chat_completion(
        self, messages, max_tokens=None, temperature=None, **kw
    ):
        reasoning_enabled = True
        if kw.get("reasoning_effort") in {"none", "minimal"}:
            reasoning_enabled = False
        template_kwargs = kw.get("chat_template_kwargs")
        if (
            isinstance(template_kwargs, dict)
            and template_kwargs.get("enable_thinking") is False
        ):
            reasoning_enabled = False
        if not reasoning_enabled:
            return "NO_MATCH"
        if max_tokens is not None and max_tokens < _REASONING_TOKENS + 8:
            return ""
        return "NO_MATCH"


async def test_no_match_reachable_under_reasoning_client():
    """AC6: evaluator must budget for (or disable) reasoning tokens."""
    saved_fail_open_count = RetrievalEvaluator.fail_open_count
    try:
        verdict = await RetrievalEvaluator(_ReasoningFakeClient()).evaluate(
            "q", [{"text": "unrelated"}]
        )
    finally:
        # The fail-open branch increments this class counter; restore it so
        # the base run does not leak state into other suites.
        RetrievalEvaluator.fail_open_count = saved_fail_open_count
    assert verdict == "NO_MATCH"


def test_fail_open_signal_has_production_reader(tmp_path):
    """AC7 (amended per #941 AC2): "production reader" means a LIVE CALL PATH
    — the three record_evaluator_fail_open call sites in
    retrieval_evaluator.py feeding telemetry's recorder — and the exposition
    actually binding the counter into /metrics text. The old shape was a
    substring scan over files that EXCLUDED the production call sites, so
    deleting all three calls left it green (reviewer-executed mutation,
    PR #931 review); this binding goes red under both that deletion and a
    metrics_text() un-wiring while the call sites remain."""
    import ast as _ast
    from pathlib import Path as _Path

    import app.services.telemetry as telemetry_module

    backend_dir = _Path(__file__).resolve().parents[1]
    evaluator_path = backend_dir / "app" / "services" / "retrieval_evaluator.py"
    evaluator_tree = _ast.parse(
        evaluator_path.read_text(encoding="utf-8"), filename=str(evaluator_path)
    )
    call_sites = [
        node.lineno
        for node in _ast.walk(evaluator_tree)
        if isinstance(node, _ast.Call)
        and isinstance(node.func, _ast.Attribute)
        and node.func.attr == "record_evaluator_fail_open"
    ]
    assert len(call_sites) >= 3, (
        "the production fail-open call sites live in retrieval_evaluator.py "
        f"(exception / empty_response / unexpected_response); found only "
        f"{len(call_sites)} at {call_sites}"
    )

    # The reader: a recorded fail-open must reach the /metrics exposition —
    # recorder definition alone is not a read path.
    tel = telemetry_module.Telemetry(
        enabled=True, registry_dir=tmp_path / "telreg"
    )
    tel.record_evaluator_fail_open("exception")
    assert (
        'ragapp_retrieval_evaluator_fail_open_total{reason="exception"} 1'
        in tel.metrics_text()
    ), (
        "a recorded fail-open must appear in metrics_text() — the "
        "recorder-to-exposition read path is the wiring AC7 pins"
    )
