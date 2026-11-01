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


def test_fail_open_signal_has_production_reader():
    """AC7: some production file other than the evaluator reads fail_open."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    excluded = "services/retrieval_evaluator.py"

    count = 0
    for path in sorted(app_dir.rglob("*.py")):
        if path.relative_to(app_dir).as_posix() == excluded:
            continue
        if "fail_open" in path.read_text(encoding="utf-8"):
            count += 1

    assert count >= 1
