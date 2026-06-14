"""Verify each LLMClient owns its own circuit breaker.

After the dual-mode (Instant/Thinking) refactor, the LLMClient no longer
uses the module-level ``llm_cb`` singleton inside its methods. Each
instance constructs its own ``AsyncCircuitBreaker`` so that failures on
one backend cannot trip the breaker for another.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import asyncio
import inspect
from unittest.mock import patch

import pytest

from app.services.circuit_breaker import AsyncCircuitBreaker, CircuitBreakerState
from app.services.llm_client import (
    LLMClient,
    create_instant_client,
    create_thinking_client,
)


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.llm_client.assert_url_safe"):
        yield


def test_per_instance_circuit_breaker_distinct_objects():
    """Two LLMClient instances must hold distinct circuit breaker instances."""
    a = LLMClient()
    b = LLMClient()
    assert isinstance(a._circuit_breaker, AsyncCircuitBreaker)
    assert isinstance(b._circuit_breaker, AsyncCircuitBreaker)
    assert a._circuit_breaker is not b._circuit_breaker


def test_thinking_and_instant_factories_have_distinct_breakers():
    """Factory-created Thinking and Instant clients must have isolated breakers and distinct names."""
    thinking = create_thinking_client()
    instant = create_instant_client()
    assert thinking._circuit_breaker is not instant._circuit_breaker
    assert thinking._circuit_breaker.name == "llm_thinking"
    assert instant._circuit_breaker.name == "llm_instant"


def test_breaker_failure_on_one_does_not_trip_other():
    """Recording failures on instance A's breaker must NOT affect instance B's state."""
    a = LLMClient()
    b = LLMClient()

    async def trip_a():
        # fail_max=5 — record 5 failures to trip A.
        async with a._circuit_breaker._lock:
            for _ in range(5):
                a._circuit_breaker.record_failure()

    asyncio.run(trip_a())

    assert a._circuit_breaker.current_state == CircuitBreakerState.OPEN
    assert b._circuit_breaker.current_state == CircuitBreakerState.CLOSED


def test_reconfigure_updates_base_url_and_model_in_place():
    """LLMClient.reconfigure must hot-swap base_url and model without recreating the client."""
    c = LLMClient(base_url="http://old.example:1234", model="old-model")
    breaker_ref = c._circuit_breaker
    c.reconfigure(base_url="http://new.example:5678", model="new-model")
    assert c.base_url == "http://new.example:5678"
    assert c.model == "new-model"
    # Breaker reference is preserved — no recreation.
    assert c._circuit_breaker is breaker_ref


def test_reconfigure_resets_open_breaker_when_endpoint_changes():
    """Reconfiguring to a new endpoint must reset a previously opened breaker."""
    c = LLMClient(base_url="http://old.example:1234", model="old-model")

    async def trip():
        async with c._circuit_breaker._lock:
            for _ in range(5):
                c._circuit_breaker.record_failure()

    asyncio.run(trip())
    assert c._circuit_breaker.current_state == CircuitBreakerState.OPEN

    c.reconfigure(base_url="http://new.example:5678")
    assert c._circuit_breaker.current_state == CircuitBreakerState.CLOSED
    assert c._circuit_breaker.fail_counter == 0


def test_reconfigure_resets_open_breaker_when_model_changes():
    """Reconfiguring to a new model must reset a previously opened breaker."""
    c = LLMClient(base_url="http://old.example:1234", model="old-model")

    async def trip():
        async with c._circuit_breaker._lock:
            for _ in range(5):
                c._circuit_breaker.record_failure()

    asyncio.run(trip())
    assert c._circuit_breaker.current_state == CircuitBreakerState.OPEN

    c.reconfigure(model="new-model")
    assert c._circuit_breaker.current_state == CircuitBreakerState.CLOSED


def test_reconfigure_noop_preserves_breaker_state():
    """Reconfiguring with the same values must not reset an opened breaker."""
    c = LLMClient(base_url="http://old.example:1234", model="old-model")

    async def trip():
        async with c._circuit_breaker._lock:
            for _ in range(5):
                c._circuit_breaker.record_failure()

    asyncio.run(trip())
    assert c._circuit_breaker.current_state == CircuitBreakerState.OPEN

    # Same values — should be a no-op.
    c.reconfigure(base_url="http://old.example:1234", model="old-model")
    assert c._circuit_breaker.current_state == CircuitBreakerState.OPEN


def test_llm_client_docstring_max_tokens_defaults_match():
    """Verify max_tokens parameter defaults match their docstrings (default: 32768).

    Phase 3 Task 3.2 fixed two stale docstring defaults from 2048 to 32768.
    This test ensures the defaults in signatures and docstrings stay in sync.
    """
    # chat_completion — signature default
    sig = inspect.signature(LLMClient.chat_completion)
    assert sig.parameters["max_tokens"].default == 32768, (
        f"chat_completion max_tokens default is {sig.parameters['max_tokens'].default}, expected 32768"
    )
    # chat_completion — docstring contains "default: 32768"
    assert "default: 32768" in (LLMClient.chat_completion.__doc__ or ""), (
        "chat_completion docstring does not mention 'default: 32768'"
    )
    # Verify it does NOT contain the stale value
    assert "default: 2048" not in (LLMClient.chat_completion.__doc__ or ""), (
        "chat_completion docstring still contains stale 'default: 2048'"
    )

    # chat_completion_stream — signature default
    sig2 = inspect.signature(LLMClient.chat_completion_stream)
    assert sig2.parameters["max_tokens"].default == 32768, (
        f"chat_completion_stream max_tokens default is {sig2.parameters['max_tokens'].default}, expected 32768"
    )
    # chat_completion_stream — docstring contains "default: 32768"
    assert "default: 32768" in (LLMClient.chat_completion_stream.__doc__ or ""), (
        "chat_completion_stream docstring does not mention 'default: 32768'"
    )
    # Verify it does NOT contain the stale value
    assert "default: 2048" not in (LLMClient.chat_completion_stream.__doc__ or ""), (
        "chat_completion_stream docstring still contains stale 'default: 2048'"
    )
