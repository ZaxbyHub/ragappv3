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
    @pytest.mark.parametrize(
        "payload",
        [
            "[1, 2, 3]",  # list
            "5",  # scalar int
            '"text"',  # scalar string
            "null",  # JSON null
            "true",  # JSON bool
        ],
        ids=["list", "int", "str", "null", "bool"],
    )
    async def test_non_dict_data_payloads_are_skipped_not_crashed(
        self, caplog, payload
    ):
        """PRR-008: the guard must cover EVERY non-dict JSON payload, not just
        arrays. A narrowing to `isinstance(data, list)` passed the original
        array-only test while `data: 5` then crashed with AttributeError."""
        client = _stream_client()
        _install_stream(
            client,
            [
                'data: {"choices": [{"delta": {"content": "ok"}}]}',
                "",
                f"data: {payload}",
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
        # The content delta still flows; the non-dict payload is skipped, and
        # no AttributeError escapes (pre-fix it crashed the generator).
        assert chunks == ["ok"]
        assert client.last_metrics.get("status") == "ok"
        assert not [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ], "a non-dict payload must not produce an ERROR log either"


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
            # PRR-021: scope the filter to this logger. Unscoped, a
            # third-party WARNING matching /reason|think/ inside the window
            # would false-fail this guard.
            if r.name == "app.services.llm_client"
            and r.levelno >= logging.WARNING
            and re.search(r"(?i)reason|think", r.getMessage())
        ]
        assert hits == []
        assert isinstance(events[0], lc.ReasoningDelta)
        assert events[1] == "answer"


class TestTailDrainIsBounded:
    """PRR-022/PRR-018: the post-[DONE] drain must be BOUNDED. A server that
    keeps the body open after the marker (keep-alive trickle, missing chunked
    terminator) must not pin the request: the drain deadline is the only thing
    that stops it, so it is pinned here."""

    @pytest.mark.asyncio
    async def test_drain_gives_up_on_a_non_terminating_tail(self):
        import asyncio as _asyncio
        import time as _time

        client = _stream_client()

        class HangingAfterDone:
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"choices": [{"delta": {"content": "answer"}}]}'
                yield ""
                yield "data: [DONE]"
                yield ""
                # Never ends: a keep-alive trickle that resets any per-read
                # timeout. The drain deadline must still release the caller.
                while True:
                    await _asyncio.sleep(0.01)
                    yield ": keepalive"

        class CM:
            async def __aenter__(self):
                return HangingAfterDone()

            async def __aexit__(self, *a):
                return False

        client._client = mock.MagicMock()
        client._client.stream.return_value = CM()

        # Shrink the deadline so the test does not wait the production 5s.
        with mock.patch.object(lc, "_STREAM_TAIL_DRAIN_TIMEOUT", 0.2):
            started = _time.perf_counter()
            events = []
            async for ev in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            ):
                events.append(ev)
            elapsed = _time.perf_counter() - started

        assert events == ["answer"]
        assert elapsed < 3.0, f"drain was not bounded ({elapsed:.1f}s)"
        assert client.last_metrics.get("status") == "ok"
        assert client._circuit_breaker._fail_counter == 0


class TestCancelAfterDoneIsSuccess:
    """PRR-023: a consumer cancelled AFTER the generation completed must not
    leave last_metrics reporting an error for a delivered answer."""

    @pytest.mark.asyncio
    async def test_cancellation_after_done_records_success(self):
        import asyncio as _asyncio

        client = _stream_client()

        class SlowTail:
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"choices": [{"delta": {"content": "answer"}}]}'
                yield ""
                yield "data: [DONE]"
                yield ""
                await _asyncio.sleep(30)  # hangs in the drain

        class CM:
            async def __aenter__(self):
                return SlowTail()

            async def __aexit__(self, *a):
                return False

        client._client = mock.MagicMock()
        client._client.stream.return_value = CM()

        async def consume():
            async for _ in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            ):
                pass

        task = _asyncio.create_task(consume())
        await _asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task

        assert client.last_metrics.get("status") == "ok", (
            "a completed generation cancelled in the drain must not be reported "
            "as an error"
        )
        assert client._circuit_breaker._fail_counter == 0
