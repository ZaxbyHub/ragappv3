"""Extra coverage for issue #719 changes that cannot join the frozen c14 files.

Covers: (a) the reranker_max_batch validator, (b) the non-dict SSE payload
skip, (c) the F1 regression guard — the no-think leak warning must not crash
or fire for a thinking-enabled (reconfigured-to-None) client.
"""

import logging
import re
import unittest.mock as mock

import pytest

from app.services import llm_client as lc


class TestRerankerMaxBatchValidator:
    def _settings(self, **overrides):
        from app.config import Settings

        base = dict(
            users_enabled=False,
            admin_secret_token="test-admin-key-0123456789abcdef",
            jwt_secret_key="test-jwt-key-0123456789abcdef0123456789abcdef",
        )
        base.update(overrides)
        return Settings(**base)

    def test_zero_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._settings(reranker_max_batch=0)

    def test_one_accepted(self):
        assert self._settings(reranker_max_batch=1).reranker_max_batch == 1


def _stream_client(chat_template_kwargs=None):
    """Build an LLMClient whose stream is served by a fake SSE response."""
    with mock.patch.object(lc, "assert_url_safe"):
        client = lc.LLMClient(
            base_url="http://llm-test-host:1234/v1",
            model="m",
            chat_template_kwargs=chat_template_kwargs,
        )
    return client


def _install_stream(client, lines):
    class Resp:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for ln in lines:
                yield ln

    class CM:
        async def __aenter__(self):
            return Resp()

        async def __aexit__(self, *a):
            return False

    client._client = mock.MagicMock()
    client._client.stream.return_value = CM()


class TestNonDictSSEPayloadSkipped:
    @pytest.mark.asyncio
    async def test_bare_json_array_data_line_is_skipped_not_crashed(self, caplog):
        client = _stream_client()
        _install_stream(
            client,
            [
                'data: {"choices": [{"delta": {"content": "ok"}}]}',
                "",
                "data: [1, 2, 3]",
                "",
                "data: [DONE]",
                "",
            ],
        )
        chunks = []
        with caplog.at_level(logging.ERROR, logger="app.services.llm_client"):
            async for ev in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            ):
                chunks.append(ev)
        # The content delta still flows; the array payload is skipped, and no
        # AttributeError escapes (pre-fix it crashed the generator).
        assert chunks == ["ok"]
        assert client.last_metrics.get("status") == "ok"


class TestNoThinkWarningRegressionGuard:
    @pytest.mark.asyncio
    async def test_reconfigured_none_kwargs_no_warning_no_crash(self, caplog):
        """F1 guard: after reconfigure(chat_template_kwargs=None) the client is
        thinking-enabled; a reasoning delta must neither crash (the bare .get
        on None) nor emit the no-think leak warning."""
        client = _stream_client(chat_template_kwargs={"enable_thinking": False})
        client.reconfigure(chat_template_kwargs=None)
        assert client.chat_template_kwargs is None
        _install_stream(
            client,
            [
                'data: {"choices": [{"delta": {"reasoning_content": "thinking"}}]}',
                "",
                'data: {"choices": [{"delta": {"content": "answer"}}]}',
                "",
                "data: [DONE]",
                "",
            ],
        )
        events = []
        with caplog.at_level(logging.WARNING, logger="app.services.llm_client"):
            async for ev in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            ):
                events.append(ev)
        hits = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and re.search(r"(?i)reason|think", r.getMessage())
        ]
        assert hits == []
        assert isinstance(events[0], lc.ReasoningDelta)
        assert events[1] == "answer"
