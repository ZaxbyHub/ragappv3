"""
LLM service health check helper.

Provides health check functionality for embedding and chat services
with short timeouts suitable for health check endpoints.
"""
from typing import Any, Dict, Optional

import httpx

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

        Calls EmbeddingService.embed_single("ping") with a short timeout.

        Returns:
            Status dict with:
            - ok: bool indicating if the service is healthy
            - error: Error message if not healthy, None otherwise
        """
        service = self._embedding_service

        try:
            if service is None:
                # Self-created probe service gets the checker's short timeout.
                # An injected service is the shared production instance and
                # keeps its own configured timeout: temporarily clobbering it
                # to the checker's 5s both falsely fails slow-but-healthy
                # embedding backends and races concurrent real embed calls
                # that read the same mutable attribute.
                service = EmbeddingService()
                service.timeout = self.timeout

            # Attempt to embed a simple ping message
            await service.embed_single("ping")
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
        """
        client = self._llm_client
        created_locally = False

        try:
            if client is None:
                client = LLMClient(timeout=self.timeout)
                created_locally = True
                await client.start()

            # Attempt a simple chat completion
            messages = [{"role": "user", "content": "ping"}]
            await client.chat_completion(messages)
            return {"ok": True, "error": None}

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
        """Send a minimal ping through ``client`` and return True iff successful."""
        try:
            await client.chat_completion(
                [{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True
        except Exception:
            return False

    async def check_chat_modes(self) -> Dict[str, bool]:
        """Probe both the Thinking and Instant endpoints independently.

        Returns:
            ``{"thinking": bool, "instant": bool}``. A missing client is
            reported as ``False`` so the frontend fails closed.
        """
        thinking_ok = (
            await self._probe_client(self._thinking_client)
            if self._thinking_client is not None
            else False
        )
        instant_ok = (
            await self._probe_client(self._instant_client)
            if self._instant_client is not None
            else False
        )
        return {"thinking": thinking_ok, "instant": instant_ok}

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
