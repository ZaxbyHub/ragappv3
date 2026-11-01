"""Acceptance checks for issue #711 (C-06) — planner single-facet echo contract.

Freezes the #711 acceptance criterion AC6 for QueryPlanner:

- AC6: when the planner's own prompt shows the model a single-facet example
  (``["<the query>"]``) and a compliant model echoes exactly that example
  back, the resulting plan must contain exactly one item — the original
  query. RED at the #711 base (the raw query is prepended on top of the
  echoed — escaped — copy, yielding a two-item plan), GREEN once #711 lands.

The fake LLM does NOT hardcode the escaped example: it reads the user
message the planner actually rendered, extracts the text after the
"If the question has only one facet, respond with:" marker, and returns it
verbatim — i.e. the model complies with the prompt exactly as written.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services.query_transformer import QueryPlanner

_SINGLE_FACET_MARKER = "If the question has only one facet, respond with:\n"

# Multi-clause ("and") and well over 60 chars — definitely NOT a simple
# query, so plan() takes the LLM path.
_QUERY = (
    "What's the company's travel reimbursement policy and how does it "
    "apply to contractors abroad?"
)


@pytest.mark.asyncio
async def test_echo_of_single_facet_example_yields_single_item_plan(monkeypatch):
    """AC6: a compliant single-facet echo must produce a one-item plan."""
    monkeypatch.setattr(settings, "redis_url", None)

    async def fake_chat_completion(*args, **kwargs):
        """Model that complies: echo the single-facet example verbatim."""
        messages = kwargs.get("messages") or args[0]
        user_content = messages[1]["content"]
        start = user_content.index(_SINGLE_FACET_MARKER) + len(_SINGLE_FACET_MARKER)
        return user_content[start:]

    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(side_effect=fake_chat_completion)

    planner = QueryPlanner(llm)
    plan = await planner.plan(_QUERY)

    assert len(plan) == 1, (
        "echoing the single-facet example must yield a one-item plan, got: "
        f"{plan}"
    )
