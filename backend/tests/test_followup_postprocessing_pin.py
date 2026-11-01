"""Pin (#928 AC4) — rewrite_followup response post-processing.

Pins rewrite_followup post-processing (first-line selection,
quote/bracket stripping, minimum-length rejection) per #928 AC4 — no
master test asserts the rewrite RESULT value — salvaged from superseded
PR #990 and verified green against master's implementation. The blank
user-turn guard of #992 must not disturb this happy path.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services.query_transformer import QueryTransformer

HISTORY_HEALTHY = [
    {"role": "user", "content": "What is the refund policy?"},
    {"role": "assistant", "content": "Refunds take 5 days."},
]


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
async def test_nonempty_response_postprocessing_unchanged(followup_settings):
    """AC4: non-empty responses keep the existing post-processing.

    PRESERVING — the first line of a multiline response wins, surrounding
    quotes/brackets are stripped, and a result shorter than 3 chars after
    stripping is rejected with ``None``.
    """
    # First-line selection + surrounding-quote strip.
    llm = _make_llm('  "What is the refund policy?"  \ndiscarded preamble line')
    transformer = QueryTransformer(llm)
    rewritten = await transformer.rewrite_followup(
        query="try again", chat_history=HISTORY_HEALTHY
    )
    assert rewritten == "What is the refund policy?"

    # Minimum-length rejection after quote stripping.
    llm_short = _make_llm('"hi"')
    transformer_short = QueryTransformer(llm_short)
    rewritten_short = await transformer_short.rewrite_followup(
        query="try again", chat_history=HISTORY_HEALTHY
    )
    assert rewritten_short is None
