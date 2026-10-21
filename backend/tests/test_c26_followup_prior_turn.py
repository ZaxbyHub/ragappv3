"""Acceptance checks for issue #928 (C-26) — follow-up prior-turn degenerates.

These checks freeze the #928 acceptance criteria for the prior-turn scan and
response post-processing of ``QueryTransformer.rewrite_followup``:

- AC1: an empty newest assistant message (a streaming turn that errored
  after opening the assistant message, or a client-side empty placeholder)
  must not suppress older non-empty assistant turns — the scan keeps
  walking and uses the older non-empty answer as rewrite context. RED at
  the #928 base (the empty string anchors ``prior_assistant`` and the
  search stops), GREEN once #928 lands.
- AC2: a whitespace-only rewrite LLM response returns ``None`` (the
  documented failure signal) instead of raising ``IndexError`` from
  ``"".splitlines()[0]``. RED at the #928 base, GREEN once #928 lands.
- AC3: history whose only assistant message is empty still rewrites from
  the prior user turn alone. GREEN at the #928 base and at the fix head —
  PRESERVING, so the emptiness guard cannot accidentally gate the whole
  rewrite on assistant content.
- AC4: a non-empty rewrite response keeps its post-processing (first-line
  selection, quote/bracket stripping, minimum-length rejection) unchanged.
  PRESERVING — the whitespace guard must not alter the happy path.

Harness follows the recording-LLM pattern of test_c06_followup_escape.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.query_transformer import QueryTransformer

# Newest assistant turn is empty (streaming error / client placeholder);
# the older turn carries the real answer.
HISTORY_EMPTY_NEWEST_ASSISTANT = [
    {"role": "user", "content": "What is the refund policy?"},
    {"role": "assistant", "content": "Refunds take 5 days."},
    {"role": "user", "content": "and for enterprise accounts?"},
    {"role": "assistant", "content": ""},
]

HISTORY_ONLY_ASSISTANT_EMPTY = [
    {"role": "user", "content": "What is the refund policy?"},
    {"role": "assistant", "content": ""},
]

HISTORY_HEALTHY = [
    {"role": "user", "content": "What is the refund policy?"},
    {"role": "assistant", "content": "Refunds take 5 days."},
]


def _make_recording_transformer(return_value: str):
    """Build a QueryTransformer whose LLM records the messages sent to it."""
    mock_llm = MagicMock()
    mock_llm.chat_completion = AsyncMock(return_value=return_value)
    with patch("app.services.query_transformer.settings") as mock_settings:
        mock_settings.redis_url = None
        mock_settings.chat_model = "test-model"
        mock_settings.query_transform_temperature = 0.0
        transformer = QueryTransformer(llm_client=mock_llm)
    return transformer, mock_llm


def _captured_user_prompt(mock_llm) -> str:
    """Return the user-message content of the (single) rewrite LLM call."""
    assert mock_llm.chat_completion.call_count == 1
    messages = mock_llm.chat_completion.call_args[1]["messages"]
    return messages[1]["content"]


@pytest.mark.asyncio
async def test_empty_newest_assistant_falls_back_to_older_turn():
    """AC1: an empty newest assistant turn must not shadow older turns.

    At the #928 base the reversed scan anchors ``prior_assistant`` on the
    empty string and stops, so the rewrite prompt carries an empty
    ``<assistant_answer>`` block. The older non-empty answer must reach the
    prompt instead.
    """
    transformer, mock_llm = _make_recording_transformer(
        "What is the refund policy for enterprise accounts?"
    )

    rewritten = await transformer.rewrite_followup(
        query="try again", chat_history=HISTORY_EMPTY_NEWEST_ASSISTANT
    )

    assert rewritten is not None, (
        "the rewrite must still fire when the newest assistant turn is empty"
    )
    user_content = _captured_user_prompt(mock_llm)
    assert "Refunds take 5 days." in user_content, (
        "the older non-empty assistant turn must be used as rewrite context; "
        "the empty newest assistant turn must not suppress it"
    )


@pytest.mark.asyncio
async def test_whitespace_only_response_returns_none():
    """AC2: a whitespace-only LLM response returns None, not IndexError.

    At the #928 base ``if not response:`` passes a whitespace-only string,
    then ``response.strip().splitlines()[0]`` indexes an empty list and the
    ``IndexError`` escapes the method, violating the documented
    return-``None``-on-failure contract.
    """
    transformer, _ = _make_recording_transformer("   \n \t ")

    rewritten = await transformer.rewrite_followup(
        query="try again", chat_history=HISTORY_HEALTHY
    )

    assert rewritten is None


@pytest.mark.asyncio
async def test_empty_only_assistant_still_rewrites_from_prior_user():
    """AC3: an empty-only assistant history still rewrites from the user turn.

    The emptiness of the assistant turn must not remove the prior-user
    context that is available: the rewrite is still produced (with an empty
    ``<assistant_answer>`` context block). PRESERVING — GREEN at the base.
    """
    transformer, mock_llm = _make_recording_transformer(
        "What is the refund policy?"
    )

    rewritten = await transformer.rewrite_followup(
        query="try again", chat_history=HISTORY_ONLY_ASSISTANT_EMPTY
    )

    assert rewritten is not None, (
        "an empty only assistant turn must not by itself return None; the "
        "rewrite must proceed from the prior user turn alone"
    )
    user_content = _captured_user_prompt(mock_llm)
    assert "What is the refund policy?" in user_content


@pytest.mark.asyncio
async def test_nonempty_response_postprocessing_unchanged():
    """AC4: non-empty responses keep the existing post-processing.

    PRESERVING — the first line of a multiline response wins, surrounding
    quotes/brackets are stripped, and a result shorter than 3 chars after
    stripping is rejected with ``None``.
    """
    # First-line selection + surrounding-quote strip.
    transformer, _ = _make_recording_transformer(
        '  "What is the refund policy?"  \ndiscarded preamble line'
    )
    rewritten = await transformer.rewrite_followup(
        query="try again", chat_history=HISTORY_HEALTHY
    )
    assert rewritten == "What is the refund policy?"

    # Minimum-length rejection after quote stripping.
    transformer_short, _ = _make_recording_transformer('"hi"')
    rewritten_short = await transformer_short.rewrite_followup(
        query="try again", chat_history=HISTORY_HEALTHY
    )
    assert rewritten_short is None
