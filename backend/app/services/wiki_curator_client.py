"""HTTP client for the optional LLM Wiki Curator (PR C).

Independent of LLMClient on purpose: the curator runs against a
separate model / endpoint and must not trip the chat circuit breaker
when it fails. We mirror LLMClient's OpenAI-compatible request shape
and reuse the same thinking-content sanitizer so the curator's output
is treated exactly like chat output downstream.

SSRF guard: every outbound call assert_curator_url_safe()'s the URL
first, even though the settings PUT validator did the same. Defense in
depth: a curator URL persisted before the guard was tightened, or a
local-model env var that flipped, must still be rejected at request
time.

JSON extraction: small local instruct models routinely emit JSON
wrapped in prose ("Sure! Here's the JSON: { ... }"). The
``extract_json`` helper is robust to that — it tries strict parse,
balanced-brace scan, and trailing-prose strip in order.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.services.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerError,
)
from app.services.curator_ssrf import (
    CuratorURLBlocked,
    assert_curator_url_safe,
)
from app.utils.assistant_sanitizer import sanitize_assistant_content

logger = logging.getLogger(__name__)


@dataclass
class CuratorPingResult:
    ok: bool
    model: str
    latency_ms: Optional[int]
    error: Optional[str] = None


class CuratorClient:
    """Async OpenAI-compatible client for the curator endpoint.

    All instance configuration (URL / model / timeout / max_tokens /
    temperature) is captured at construction so a single curator job
    can use a stable config even if the operator edits settings mid-run.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 120.0,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        breaker_name: str = "curator",
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        # Independent breaker so curator outages don't trip chat.
        self._breaker = AsyncCircuitBreaker(
            fail_max=5,
            reset_timeout=60.0,
            success_threshold=1,
            name=breaker_name,
        )

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/v1/chat/completions"):
            return self.base_url
        return self.base_url + "/v1/chat/completions"

    async def propose(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Issue an OpenAI-compatible chat completion and return the text.

        Raises:
            CuratorURLBlocked: SSRF guard fired.
            CircuitBreakerError: breaker is open.
            httpx.HTTPError: network/timeout error.
            ValueError: response didn't include a content field.
        """
        if not self.base_url or not self.model:
            raise ValueError("CuratorClient requires base_url and model")
        assert_curator_url_safe(self.base_url)

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": (
                self.temperature if temperature is None else float(temperature)
            ),
            "max_tokens": (
                self.max_tokens if max_tokens is None else int(max_tokens)
            ),
            "stream": False,
        }

        async def _do_post() -> dict:
            # SSRFSafeTransport re-validates the resolved IP at request time to
            # close the DNS-rebinding TOCTOU gap the per-call guard leaves open.
            from app.services.ssrf_transport import SSRFSafeTransport

            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                transport=SSRFSafeTransport(),
            ) as client:
                resp = await client.post(self.endpoint, json=payload)
            if resp.status_code >= 300:
                raise httpx.HTTPStatusError(
                    f"Curator returned HTTP {resp.status_code}: {resp.text[:200]}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()

        # AsyncCircuitBreaker.call records success/failure for us and
        # raises CircuitBreakerError when the breaker is open.
        data = await self._breaker.call(_do_post)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"Curator response missing content: {e}") from e

        # Reuse the chat sanitizer so <think>…</think> blocks etc. are
        # stripped exactly as they would be from a chat response.
        return sanitize_assistant_content(content or "")

    async def test_connection(self) -> CuratorPingResult:
        """Issue a tiny JSON-only ping. Used by POST /settings/curator/test
        and also exposable to operators who want to verify a config
        without touching the full curator pipeline.

        Best-effort: every error path returns a result with ok=False
        rather than raising, so callers can render the message inline.
        """
        if not self.base_url or not self.model:
            return CuratorPingResult(
                ok=False,
                model=self.model,
                latency_ms=None,
                error="Curator URL and model are required.",
            )
        try:
            assert_curator_url_safe(self.base_url)
        except CuratorURLBlocked as e:
            return CuratorPingResult(
                ok=False, model=self.model, latency_ms=None, error=str(e)
            )

        started = time.monotonic()
        try:
            text = await self.propose(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a JSON-only echo. Reply with the literal "
                            'JSON object {"ok": true} and nothing else.'
                        ),
                    },
                    {"role": "user", "content": '{"ping": true}'},
                ],
                max_tokens=16,
                temperature=0.0,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            # We accept ANY non-empty response — the chat path does the
            # real schema validation on real prompts. The ping just
            # verifies reachability + auth + non-empty completion.
            return CuratorPingResult(
                ok=bool(text),
                model=self.model,
                latency_ms=latency_ms,
                error=None if text else "Curator returned empty content.",
            )
        except CircuitBreakerError as e:
            return CuratorPingResult(
                ok=False, model=self.model, latency_ms=None, error=str(e)
            )
        except httpx.TimeoutException:
            latency_ms = int((time.monotonic() - started) * 1000)
            return CuratorPingResult(
                ok=False,
                model=self.model,
                latency_ms=latency_ms,
                error=f"Curator endpoint timed out after {self.timeout}s.",
            )
        except Exception as e:  # broad — surface to UI
            latency_ms = int((time.monotonic() - started) * 1000)
            return CuratorPingResult(
                ok=False,
                model=self.model,
                latency_ms=latency_ms,
                error=str(e)[:300],
            )


# ---------------------------------------------------------------------------
# Robust JSON extraction. Exposed at module level so other code can reuse it
# (and so it's easy to unit test without standing up a CuratorClient).
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def extract_json(text: str) -> Optional[Any]:
    """Try hard to recover a JSON object/array from a curator response.

    Strategy in order:
      1. Strict json.loads (covers well-behaved models).
      2. Strip a ```json ... ``` fenced block and parse its contents.
      3. Find the first balanced `{...}` or `[...]` at the top level
         and parse it.
      4. None — caller should treat as parse failure.
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    # 1. Strict.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 2. ```json fenced block.
    m = _JSON_FENCE_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. Balanced-brace scan.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = raw[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # fall through to next opener
    return None
