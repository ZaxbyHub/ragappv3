"""Acceptance check AC1 (issue #1209): sanitizer must not run on the event loop.

``backend/app/services/llm_client.py`` runs the regex-heavy assistant
sanitizer (``_strip_thinking_content`` -> ``sanitize_assistant_content``:
five re.sub DOTALL passes plus the line-walking wrapper stripper) INLINE
from two async surfaces:

- ``chat_completion`` (the non-stream completion path), and
- ``chat_completion_stream``'s non-SSE branch (a provider that answers the
  stream endpoint with a complete JSON body, read via
  ``await response.aread()``).

Every completion therefore blocks the event loop for the whole regex pass.
Content semantics and provider-exact token accounting are correct today —
only thread placement is wrong; the repo precedent is chat.py dispatching
sanitizer work through ``asyncio.to_thread``.

For each path, the spy wraps
``app.services.llm_client.sanitize_assistant_content`` (the name imported
into that module's namespace) and records ``threading.get_ident()`` per
call. The async test body runs on the event loop, so a ``get_ident``
captured there is the loop thread's id.

- Every recorded sanitizer thread id must DIFFER from the event-loop
  thread's id. RED at base (the inline call records the loop's own id),
  GREEN after the fix.
- The returned/yielded content must equal a direct
  ``sanitize_assistant_content(raw)`` computation on the raw provider
  content, with the thinking trace actually stripped. GREEN at base and
  after the fix — the thread-placement fix must not change content.
- Provider-exact token accounting must be unchanged: stub usage
  ``{"prompt_tokens": 5, "completion_tokens": 9}`` must land in
  ``client.last_metrics`` after the call. GREEN at base and after the fix.

Kill mutations (one per path): revert the ``chat_completion`` call site
back to the inline ``self._strip_thinking_content(content)`` (the thread
identity assertion then records the loop's own id and fails); revert the
``chat_completion_stream`` non-SSE branch call site the same way.
"""

import os
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

# Test env: tests/conftest.py sets the same values at pytest_configure; set
# them here too so importing the app modules never depends on import order.
os.environ["ADMIN_SECRET_TOKEN"] = "test-admin-key"
os.environ["USERS_ENABLED"] = "false"

from app.services.llm_client import LLMClient
from app.utils.assistant_sanitizer import sanitize_assistant_content

BASE_URL = "http://llm.invalid"
RAW_CONTENT = "<think>offloop reasoning trace 1209</think>The visible answer text."
THINK_MARKER = "offloop reasoning trace 1209"
ANSWER_TEXT = "The visible answer text."
USAGE = {"prompt_tokens": 5, "completion_tokens": 9}


class _StreamContext:
    """Stand-in for httpx's ``client.stream(...)`` async context manager."""

    def __init__(self, response: httpx.Response):
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _json_response(payload: dict) -> httpx.Response:
    request = httpx.Request("POST", f"{BASE_URL}/v1/chat/completions")
    return httpx.Response(200, request=request, json=payload)


def _completion_payload() -> dict:
    return {
        "choices": [
            {"message": {"content": RAW_CONTENT}, "finish_reason": "stop"}
        ],
        "usage": dict(USAGE),
    }


def _make_client() -> LLMClient:
    with patch("app.services.llm_client.assert_url_safe"):
        return LLMClient(base_url=BASE_URL, model="test-model")


async def test_chat_completion_sanitizer_runs_off_event_loop():
    """AC1 (non-stream path): off-loop dispatch, content, and token parity.

    RED at base: the inline ``self._strip_thinking_content(content)`` call
    in ``chat_completion`` runs the sanitizer on the event-loop thread, so
    the spy records the loop's own id and the thread-identity assertion
    fails. GREEN after the fix. The content and token assertions hold at
    both ends. Killed by reverting the ``chat_completion`` call site to the
    inline call.
    """
    loop_tid = threading.get_ident()  # async test body runs on the loop thread
    client = _make_client()
    stub = AsyncMock()
    stub.post = AsyncMock(return_value=_json_response(_completion_payload()))
    client._ensure_started = AsyncMock(return_value=stub)  # noqa: SLF001

    recorded = []

    def _spy(content: str) -> str:
        recorded.append((threading.get_ident(), content))
        return sanitize_assistant_content(content)

    with patch(
        "app.services.llm_client.sanitize_assistant_content", side_effect=_spy
    ):
        result = await client.chat_completion([{"role": "user", "content": "hi"}])

    assert len(recorded) == 1, (
        f"1209: the sanitizer must run exactly once on the non-stream path; "
        f"recorded {len(recorded)} call(s)"
    )
    spy_tid, spy_input = recorded[0]
    assert spy_tid != loop_tid, (
        "1209: _strip_thinking_content ran on the event-loop thread inside "
        "chat_completion — the sanitizer dispatch is missing (inline call)"
    )
    assert spy_input == RAW_CONTENT, (
        f"1209: the sanitizer must receive the raw provider content; got "
        f"{spy_input!r}"
    )
    assert result == sanitize_assistant_content(RAW_CONTENT), (
        "1209: the returned content must equal the direct sanitizer output "
        "(content unchanged by the thread-placement fix)"
    )
    assert THINK_MARKER not in result and ANSWER_TEXT in result, (
        f"1209: the thinking trace must actually be stripped; got {result!r}"
    )
    assert client.last_metrics["prompt_tokens"] == 5, (
        f"1209: provider-exact prompt_tokens must be unchanged; got "
        f"{client.last_metrics}"
    )
    assert client.last_metrics["completion_tokens"] == 9, (
        f"1209: provider-exact completion_tokens must be unchanged; got "
        f"{client.last_metrics}"
    )
    assert client.last_metrics["status"] == "ok"


async def test_stream_non_sse_sanitizer_runs_off_event_loop():
    """AC1 (stream non-SSE branch): off-loop dispatch, content, token parity.

    A provider that answers the stream endpoint with complete JSON
    (content-type application/json) drives ``chat_completion_stream`` into
    the non-SSE branch, whose inline ``self._strip_thinking_content(...)``
    call runs the sanitizer on the event-loop thread. RED at base (the spy
    records the loop's own id), GREEN after the fix. The content and token
    assertions hold at both ends. Killed by reverting the non-SSE branch's
    call site to the inline call.
    """
    loop_tid = threading.get_ident()  # async test body runs on the loop thread
    client = _make_client()
    stub = MagicMock()
    stub.stream = MagicMock(
        return_value=_StreamContext(_json_response(_completion_payload()))
    )
    client._ensure_started = AsyncMock(return_value=stub)  # noqa: SLF001

    recorded = []

    def _spy(content: str) -> str:
        recorded.append((threading.get_ident(), content))
        return sanitize_assistant_content(content)

    with patch(
        "app.services.llm_client.sanitize_assistant_content", side_effect=_spy
    ):
        chunks = []
        async for chunk in client.chat_completion_stream(
            [{"role": "user", "content": "hi"}]
        ):
            if isinstance(chunk, str):
                chunks.append(chunk)
        joined = "".join(chunks)

    assert len(recorded) == 1, (
        f"1209: the sanitizer must run exactly once on the stream non-SSE "
        f"path; recorded {len(recorded)} call(s)"
    )
    spy_tid, spy_input = recorded[0]
    assert spy_tid != loop_tid, (
        "1209: _strip_thinking_content ran on the event-loop thread in the "
        "chat_completion_stream non-SSE branch — the sanitizer dispatch is "
        "missing (inline call)"
    )
    assert spy_input == RAW_CONTENT, (
        f"1209: the sanitizer must receive the raw provider content; got "
        f"{spy_input!r}"
    )
    assert joined == sanitize_assistant_content(RAW_CONTENT), (
        "1209: the yielded content must equal the direct sanitizer output "
        "(content unchanged by the thread-placement fix)"
    )
    assert THINK_MARKER not in joined and ANSWER_TEXT in joined, (
        f"1209: the thinking trace must actually be stripped; got {joined!r}"
    )
    # The generator is fully consumed, so the stream summary in the finally
    # block has already written the provider-exact usage into last_metrics.
    assert client.last_metrics["prompt_tokens"] == 5, (
        f"1209: provider-exact prompt_tokens must be unchanged; got "
        f"{client.last_metrics}"
    )
    assert client.last_metrics["completion_tokens"] == 9, (
        f"1209: provider-exact completion_tokens must be unchanged; got "
        f"{client.last_metrics}"
    )
    assert client.last_metrics["status"] == "ok"
