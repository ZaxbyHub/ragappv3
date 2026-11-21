"""
LLM service health check helper.

Provides health check functionality for embedding and chat services
with short timeouts suitable for health check endpoints.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.services.embeddings import EmbeddingError, EmbeddingService
from app.services.llm_client import LLMClient, LLMError


class LLMHealthChecker:
    """
    Health checker for LLM services (embeddings and chat).

    Uses short timeouts suitable for health check endpoints.
    Supports dependency injection for optional service instances.
    """

    def __init__(
        self,
        timeout: float = 5.0,
        embedding_service: Optional[EmbeddingService] = None,
        llm_client: Optional[LLMClient] = None,
        thinking_client: Optional[LLMClient] = None,
        instant_client: Optional[LLMClient] = None,
    ):
        """
        Initialize the health checker.

        Args:
            timeout: Request timeout in seconds for health checks (default: 5.0)
            embedding_service: Optional injected EmbeddingService instance
            llm_client: Optional injected LLMClient instance (legacy single-client API)
            thinking_client: Optional injected Thinking-mode LLMClient instance
            instant_client: Optional injected Instant-mode LLMClient instance
        """
        self.timeout = timeout
        self._embedding_service = embedding_service
        # Preserve legacy single-client API: `llm_client` alone targets Thinking.
        self._llm_client = llm_client
        self._thinking_client = thinking_client or llm_client
        self._instant_client = instant_client

    async def check_embeddings(self) -> Dict[str, Any]:
        """
        Check if the embedding service is healthy.

        Calls ``EmbeddingService.embed_probe(timeout)`` — a cache-bypassing
        ping embedding with a per-request short timeout. The probe uses the
        service's shared persistent client (no new pool) and never mutates
        service state.

        Returns:
            Status dict with:
            - ok: bool indicating if the service is healthy
            - error: Error message if not healthy, None otherwise
        """
        service = self._embedding_service

        try:
            if service is None:
                service = EmbeddingService()

            # Deep-health probe (OPS-003/OPS-004, issue #494): bypasses the
            # L1/Redis caches and applies the checker's short timeout
            # per-request. The shared service and its persistent client are
            # never mutated (the old timeout field-mutation never reached
            # the already-built transport).
            await service.embed_probe(self.timeout)
            return {"ok": True, "error": None}

        except EmbeddingError as e:
            return {"ok": False, "error": f"Embedding service error: {str(e)}"}
        except httpx.TimeoutException as e:
            return {"ok": False, "error": f"Embedding service timeout: {str(e)}"}
        except Exception as e:
            return {"ok": False, "error": f"Embedding service unexpected error: {str(e)}"}

    async def check_chat(self) -> Dict[str, Any]:
        """
        Check if the chat service is healthy.

        Calls LLMClient.chat_completion([{role:'user', content:'ping'}])
        with a short timeout.

        Returns:
            Status dict with:
            - ok: bool indicating if the service is healthy
            - error: Error message if not healthy, None otherwise
            - status: "not_configured" when no chat endpoint/model is set
              (mirrors ModelChecker's first-class state, issue #717).
        """
        client = self._llm_client
        created_locally = False

        if not settings.ollama_chat_url or not settings.chat_model:
            # Same OR condition ModelChecker uses: an empty URL OR model is
            # the designed unconfigured state — report it as such instead of
            # constructing an LLMClient whose SSRF pre-check raises
            # URLBlocked("URL is empty.") into the generic except branch.
            # Issue #717 review: the emptiness check is deliberately NOT
            # gated on `client is None` — production injects a long-lived
            # client, and after a live settings-clear the parked client must
            # not mask the unconfigured state with a misleading probe error.
            return {
                "ok": False,
                "status": "not_configured",
                "error": "chat endpoint not configured (set it in Settings -> Models)",
            }

        try:
            if client is None:
                client = LLMClient(timeout=self.timeout)
                created_locally = True
                await client.start()

            # Attempt a simple chat completion. The probe payload is bounded
            # (max_tokens=1) AND the checker's short deadline is enforced
            # with asyncio.wait_for (#798 AC5 / #1179 AC1): an injected
            # production client carries its own long transport timeout
            # (LLMClient has no per-request timeout kwarg), so without the
            # wrapper a health endpoint could hang on the full 300s budget.
            messages = [{"role": "user", "content": "ping"}]
            await asyncio.wait_for(
                client.chat_completion(messages, max_tokens=1),
                timeout=self.timeout,
            )
            return {"ok": True, "error": None}

        except asyncio.TimeoutError as e:
            return {"ok": False, "error": f"Chat service timeout: {str(e)}"}
        except LLMError as e:
            return {"ok": False, "error": f"Chat service error: {str(e)}"}
        except httpx.TimeoutException as e:
            return {"ok": False, "error": f"Chat service timeout: {str(e)}"}
        except Exception as e:
            return {"ok": False, "error": f"Chat service unexpected error: {str(e)}"}
        finally:
            # Ensure locally created client is closed
            if created_locally and client is not None:
                await client.close()

    async def _probe_client(self, client: LLMClient) -> bool:
        """Send a minimal ping through ``client`` and return True iff successful.

        The checker's short deadline is enforced with asyncio.wait_for for
        the same reason as ``check_chat`` (#1179 AC1): injected production
        clients carry their own long transport timeout.
        """
        try:
            await asyncio.wait_for(
                client.chat_completion(
                    [{"role": "user", "content": "ping"}],
                    max_tokens=1,
                ),
                timeout=self.timeout,
            )
            return True
        except Exception:
            return False

    async def check_editorial(self) -> Dict[str, Any]:
        """Cheap reachability probe for the editorial desk endpoint.

        Deliberately NOT a completion: the editorial model idles between
        compiles and its host unloads it, and a first-token warm-up can take
        many minutes when the model was unloaded. A health poll
        must answer "is the server up", which GET /v1/models does even while
        a model is loading. Warming is the compile preflight's job
        (draft_room._preflight_providers), not the health check's.
        """
        if not settings.editorial_chat_url:
            return {
                "ok": False,
                "status": "not_configured",
                "error": "editorial endpoint not configured",
            }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as probe:
                resp = await probe.get(
                    settings.editorial_chat_url.rstrip("/") + "/v1/models"
                )
            if resp.status_code < 500:
                return {"ok": True, "error": None}
            return {
                "ok": False,
                "error": f"editorial endpoint returned HTTP {resp.status_code}",
            }
        except Exception as e:
            return {"ok": False, "error": f"editorial endpoint unreachable: {e}"}

    def rebind_clients(
        self,
        *,
        thinking_client: Optional[LLMClient] = None,
        instant_client: Optional[LLMClient] = None,
    ) -> None:
        """Wire late-activated clients into the checker (#1179 AC2).

        Called from ``_hot_rebind_llm_clients`` when a previously
        unconfigured boot completes an endpoint pair: the newly constructed
        client must reach ``check_chat_modes`` without a restart. Omitted
        arguments leave the stored client unchanged (no-op); clearing a
        mode is handled by the settings-pair gate in ``check_chat_modes``,
        never by rebinding None.
        """
        if thinking_client is not None:
            self._thinking_client = thinking_client
        if instant_client is not None:
            self._instant_client = instant_client

    async def check_chat_modes(self) -> Dict[str, bool]:
        """Probe both the Thinking and Instant endpoints independently.

        Returns:
            ``{"thinking": bool, "instant": bool, "editorial": bool}``. A
            missing client is reported as ``False`` so the frontend fails
            closed; ``editorial`` is the Draft Room editorial lane.

            Each mode probe is additionally gated on its CURRENT settings
            endpoint pair (#1179 AC2, the configured-to-cleared
            transition): after an operator clears a pair, the parked client
            must not be probed (it would report a mode healthy whose chat
            requests are 409-guarded). Same gate ``check_chat`` already
            applies via its not_configured branch.
        """
        thinking_ok = (
            await self._probe_client(self._thinking_client)
            if (
                self._thinking_client is not None
                and settings.ollama_chat_url
                and settings.chat_model
            )
            else False
        )
        instant_ok = (
            await self._probe_client(self._instant_client)
            if (
                self._instant_client is not None
                and settings.instant_chat_url
                and settings.instant_chat_model
            )
            else False
        )
        editorial_ok = (
            (await self.check_editorial()).get("ok", False)
        )
        return {
            "thinking": thinking_ok,
            "instant": instant_ok,
            "editorial": editorial_ok,
        }

    async def check_all(self) -> Dict[str, Any]:
        """
        Check health of all LLM services.

        Returns:
            Combined status dict with:
            - ok: bool indicating if all services are healthy
            - embeddings: Status dict from check_embeddings()
            - chat: Status dict from check_chat()
            - error: Combined error message if any service failed
        """
        embeddings_status = await self.check_embeddings()
        chat_status = await self.check_chat()

        all_ok = embeddings_status["ok"] and chat_status["ok"]

        errors = []
        if not embeddings_status["ok"]:
            errors.append(embeddings_status["error"])
        if not chat_status["ok"]:
            errors.append(chat_status["error"])

        return {
            "ok": all_ok,
            "embeddings": embeddings_status,
            "chat": chat_status,
            "error": "; ".join(errors) if errors else None
        }
