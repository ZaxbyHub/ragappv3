"""Acceptance checks (#992) — rewrite_followup blank user-turn handling.

Freezes the acceptance criteria for how ``rewrite_followup`` treats
blank / whitespace-only USER turns in chat history — the user-turn
sibling of the assistant-arm blank guard shipped by #928 (PR #988):

- AC1 (DISCRIMINATING): when the newest user message is whitespace-only
  and an older non-blank user message exists, the scan must anchor the
  rewrite on the older real question. RED at the pre-fix base
  (``2c508198``): the user branch bound any non-follow-up ``str``
  content — ``"   "`` is not a follow-up phrase — so the blank turn
  locked the scan and the sent prompt embedded an empty ``<user_query>``
  anchor with the older question absent.
- AC2 (DISCRIMINATING): when EVERY user message in history is blank or
  whitespace-only, the method must return ``None`` (the documented
  insufficient-history signal — callers fall back to the original query)
  WITHOUT issuing an LLM call. RED at the pre-fix base: truthy
  whitespace passed the ``if not prior_user`` guard, so the LLM was
  called anchored on blank content.

AC3 (PRESERVING) — real user turns keep the current scan semantics
(follow-up-phrase skipping, ``is_followup_query`` gating, assistant-turn
emptiness skipping, whitespace-only-response ``None``) — is pinned by the
existing suites: ``test_c16_followup_prior_turns.py``,
``test_sibling_service_xml_escape.py``, ``test_c16b_cache_shape_guards.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services.query_transformer import QueryTransformer

Q1 = "What is included in the enterprise pricing tier?"
A1 = (
    "Enterprise includes SSO, audit logs, and priority support."
)
FOLLOWUP = "tell me more"
REWRITE = "What does the enterprise pricing tier include?"


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
async def test_whitespace_newest_user_turn_falls_back_to_older_question(
    followup_settings,
):
    """AC1: a whitespace-only newest user turn must not shadow the older
    non-blank user question — the rewrite anchors on the older question."""
    history = [
        {"role": "user", "content": Q1},
        {"role": "assistant", "content": A1},
        {"role": "user", "content": "   "},
    ]
    llm = _make_llm(REWRITE)
    transformer = QueryTransformer(llm)

    result = await transformer.rewrite_followup(query=FOLLOWUP, chat_history=history)

    assert llm.chat_completion.call_count == 1, (
        "with an older real question available the rewrite LLM call must "
        "still happen"
    )
    messages = llm.chat_completion.call_args.kwargs["messages"]
    user_prompt = messages[1]["content"]

    assert "enterprise pricing tier" in user_prompt, (
        "the sent user prompt must anchor on the older non-blank user "
        "question; a whitespace-only newest user turn must not shadow it "
        "(the prompt embedded a blank <user_query> anchor instead)"
    )
    assert result is not None, "the rewrite itself must succeed"


@pytest.mark.asyncio
async def test_blank_only_user_history_returns_none_without_llm_call(
    followup_settings,
):
    """AC2: no non-blank user turn at all → the documented None signal,
    with zero LLM calls (nothing to anchor a rewrite on)."""
    history = [
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": A1},
        {"role": "user", "content": "  \t "},
    ]
    llm = _make_llm(REWRITE)
    transformer = QueryTransformer(llm)

    result = await transformer.rewrite_followup(query=FOLLOWUP, chat_history=history)

    assert result is None, (
        "a history whose user turns are all blank/whitespace-only must "
        "return None (insufficient history; callers fall back to the "
        f"original query), got {result!r}"
    )
    assert llm.chat_completion.call_count == 0, (
        "no LLM call may be issued when there is no non-blank user turn "
        "to anchor the rewrite on"
    )
