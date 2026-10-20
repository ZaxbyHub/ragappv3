"""Acceptance checks (C16, C1/C2) — security-boundary wording in LLM prompts.

Freezes the acceptance criteria that the QueryTransformer's LLM-facing
system prompts must carry an explicit prompt-injection boundary:

- C1 (DISCRIMINATING): the step-back system prompt must contain a
  "SECURITY BOUNDARY" section that names the "<user_query>" tag it
  governs. RED at the pre-fix base (the step-back system prompt carries
  no boundary wording at all).
- C2 (DISCRIMINATING): the HyDE system prompt must contain the same
  "SECURITY BOUNDARY" section naming "<user_query>". RED at the
  pre-fix base.

Settings fixture shape follows test_c06_stepback_failure_cache.py; the
recording-LLM harness follows test_sibling_service_xml_escape.py (the
step-back call passes ``messages`` as a keyword argument, the HyDE call
passes ``messages`` positionally).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services.query_transformer import QueryTransformer

QUERY = "What is gradient descent in neural networks?"
STEP_BACK = "What are the general concepts in machine learning?"
HYDE_PASSAGE = (
    "Gradient descent is an iterative optimization algorithm that adjusts "
    "model parameters step by step to minimize a loss function."
)


@pytest.fixture
def transformer_settings(monkeypatch):
    """Same settings shape as test_c06_stepback_failure_cache.py."""
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "stepback_enabled", True)
    monkeypatch.setattr(settings, "hyde_enabled", False)
    monkeypatch.setattr(settings, "query_transform_temperature", 0.0)
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)


@pytest.mark.asyncio
async def test_stepback_system_prompt_names_security_boundary(
    transformer_settings,
):
    """C1: step-back system prompt must declare a SECURITY BOUNDARY for
    <user_query>-wrapped untrusted content."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)
    transformer = QueryTransformer(llm)

    await transformer.transform(QUERY)

    assert llm.chat_completion.call_count >= 1, (
        "the step-back path must have issued its LLM call"
    )
    # With hyde_enabled=False exactly one call happens (the step-back one),
    # and the step-back call passes messages= as a keyword argument.
    step_back_call = llm.chat_completion.call_args_list[0]
    messages = step_back_call.kwargs["messages"]
    system_prompt = messages[0]["content"]

    assert "SECURITY BOUNDARY" in system_prompt, (
        "the step-back system prompt must contain an explicit "
        "'SECURITY BOUNDARY' section instructing the model to treat "
        "tag-wrapped user content as untrusted data"
    )
    assert "<user_query>" in system_prompt, (
        "the step-back SECURITY BOUNDARY section must name the "
        "'<user_query>' tag it governs"
    )


@pytest.mark.asyncio
async def test_hyde_system_prompt_names_security_boundary(
    transformer_settings,
):
    """C2: HyDE system prompt must declare a SECURITY BOUNDARY for
    <user_query>-wrapped untrusted content."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=HYDE_PASSAGE)
    transformer = QueryTransformer(llm)

    await transformer.generate_hyde(QUERY)

    assert llm.chat_completion.call_count == 1, (
        "generate_hyde must have issued exactly one LLM call"
    )
    # generate_hyde passes messages POSITIONALLY (first positional arg).
    call_args = llm.chat_completion.call_args
    messages = call_args[0][0]
    system_prompt = messages[0]["content"]

    assert "SECURITY BOUNDARY" in system_prompt, (
        "the HyDE system prompt must contain an explicit "
        "'SECURITY BOUNDARY' section instructing the model to treat "
        "tag-wrapped user content as untrusted data"
    )
    assert "<user_query>" in system_prompt, (
        "the HyDE SECURITY BOUNDARY section must name the "
        "'<user_query>' tag it governs"
    )
