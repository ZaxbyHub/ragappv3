"""Acceptance checks (C16, C12/C13/C14) — rewrite_followup prior-turn
handling.

Freezes the acceptance criteria for how ``rewrite_followup`` selects
prior context from chat history and how it degrades on degenerate LLM
output:

- C12 (DISCRIMINATING): when history is [user Q1, assistant A1
  (non-empty), assistant "", user "tell me more"], the sent user prompt
  must contain a non-empty excerpt of A1 — an EMPTY newer assistant
  message must not shadow the older real answer. RED at the pre-fix base
  (the scan binds the first — empty — assistant turn it meets, so A1 is
  dropped and <assistant_answer> is sent empty).
- C13 (DISCRIMINATING): a whitespace-only LLM rewrite must RETURN None,
  not raise (at the base ``"".splitlines()[0]`` raises IndexError).
- C14: history whose ONLY assistant message is empty must still produce
  a rewrite anchored on the prior USER turn (the empty assistant must
  not remove prior-user context). Verified GREEN at the pre-fix base —
  reported as PRESERVING (see the trace report; do not force a fake
  RED).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services.query_transformer import QueryTransformer

Q1 = "What is the retention policy for archived documents?"
A1 = (
    "Archived documents are retained for seven years, then purged "
    "automatically by the nightly job."
)
FOLLOWUP = "tell me more"
REWRITE = (
    "What is the retention policy for archived documents and how are they "
    "purged?"
)


@pytest.fixture
def followup_settings(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_temperature", 0.0)


def _make_llm(return_value):
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=return_value)
    return llm


@pytest.mark.asyncio
async def test_empty_assistant_turn_does_not_shadow_prior_answer(
    followup_settings,
):
    """C12: the older non-empty assistant turn must reach the prompt."""
    history = [
        {"role": "user", "content": Q1},
        {"role": "assistant", "content": A1},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": FOLLOWUP},
    ]
    llm = _make_llm(REWRITE)
    transformer = QueryTransformer(llm)

    result = await transformer.rewrite_followup(query=FOLLOWUP, chat_history=history)

    assert llm.chat_completion.call_count == 1, (
        "rewrite_followup must have issued its single LLM call"
    )
    messages = llm.chat_completion.call_args.kwargs["messages"]
    user_prompt = messages[1]["content"]

    assert "seven years" in user_prompt, (
        "the sent user prompt must carry a non-empty excerpt of the older "
        "non-empty assistant answer (A1); an empty newer assistant message "
        "must not shadow it — the <assistant_answer> context was sent empty"
    )
    assert result is not None, "the rewrite itself must succeed"


@pytest.mark.asyncio
async def test_whitespace_only_rewrite_returns_none(followup_settings):
    """C13: a whitespace-only LLM rewrite degrades to None, never a raise."""
    history = [
        {"role": "user", "content": Q1},
        {"role": "assistant", "content": A1},
        {"role": "user", "content": FOLLOWUP},
    ]
    llm = _make_llm("   \n  \t ")
    transformer = QueryTransformer(llm)

    try:
        result = await transformer.rewrite_followup(
            query=FOLLOWUP, chat_history=history
        )
    except IndexError as exc:
        pytest.fail(
            "rewrite_followup raised IndexError on a whitespace-only LLM "
            "response (it must return None so the caller falls back to the "
            f"original query): {exc!r}"
        )
    assert result is None, (
        "a whitespace-only LLM rewrite must yield None (caller falls back "
        f"to the original query), got {result!r}"
    )


@pytest.mark.asyncio
async def test_empty_assistant_does_not_block_prior_user_context(
    followup_settings,
):
    """C14: an empty-only assistant history still rewrites from the prior
    user turn (the empty assistant must not remove prior-user context)."""
    history = [
        {"role": "user", "content": Q1},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": FOLLOWUP},
    ]
    llm = _make_llm(REWRITE)
    transformer = QueryTransformer(llm)

    result = await transformer.rewrite_followup(query=FOLLOWUP, chat_history=history)

    assert llm.chat_completion.call_count == 1, (
        "the prior user turn alone is enough signal for the rewrite LLM call"
    )
    messages = llm.chat_completion.call_args.kwargs["messages"]
    user_prompt = messages[1]["content"]

    assert result is not None, (
        "an empty assistant turn must not block the rewrite — the prior "
        "USER turn still carries the subject matter"
    )
    assert "retention policy" in user_prompt, (
        "the prior user turn (Q1) must still be included as context even "
        "when the only assistant turn is empty"
    )
