"""
Runtime tests for the TEI startup model-validation helper in lifespan.py
(`_validate_tei_embedding_model`).

The helper probes the embedding server's /info endpoint (at the host root) and
raises RuntimeError on a genuine model-id mismatch, while treating network
failures, SSRF blocks, and non-200 responses as non-fatal "skip validation".
"""
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors sibling test modules)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.lifespan as lifespan_mod
from app.services.ssrf import URLBlocked


def _svc(url):
    svc = MagicMock()
    svc.embeddings_url = url
    return svc


def _resp(status_code, json_value=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_value if json_value is not None else {}
    return r


def _client_patch(response=None, get_side_effect=None):
    """Patch httpx.AsyncClient with an async-context-manager mock.

    Returns (patcher, client) where client.get is an AsyncMock.
    """
    client = MagicMock()
    client.get = AsyncMock(return_value=response, side_effect=get_side_effect)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return patch("httpx.AsyncClient", return_value=cm), client


@pytest.mark.asyncio
async def test_tei_model_match_passes_and_probes_root_info():
    p_client, client = _client_patch(
        _resp(200, {"model_id": "microsoft/harrier-oss-v1-0.6b"})
    )
    with patch.object(lifespan_mod, "settings") as s, patch.object(
        lifespan_mod, "assert_url_safe"
    ), p_client:
        s.embedding_model = "microsoft/harrier-oss-v1-0.6b"
        # Native /embed URL — /info must be derived at the server root.
        await lifespan_mod._validate_tei_embedding_model(
            _svc("http://tei.local:8080/embed")
        )

    client.get.assert_awaited_once()
    assert client.get.call_args[0][0] == "http://tei.local:8080/info"


@pytest.mark.asyncio
async def test_tei_lenient_last_segment_match_passes():
    p_client, client = _client_patch(
        _resp(200, {"model_id": "microsoft/harrier-oss-v1-0.6b"})
    )
    with patch.object(lifespan_mod, "settings") as s, patch.object(
        lifespan_mod, "assert_url_safe"
    ), p_client:
        s.embedding_model = "harrier-oss-v1-0.6b"  # bare leaf, no org prefix
        await lifespan_mod._validate_tei_embedding_model(
            _svc("http://tei.local:8080")
        )

    assert client.get.call_args[0][0] == "http://tei.local:8080/info"


@pytest.mark.asyncio
async def test_tei_model_mismatch_raises_runtime_error():
    p_client, _ = _client_patch(_resp(200, {"model_id": "BAAI/bge-m3"}))
    with patch.object(lifespan_mod, "settings") as s, patch.object(
        lifespan_mod, "assert_url_safe"
    ), p_client:
        s.embedding_model = "nomic-embed-text"
        with pytest.raises(RuntimeError):
            await lifespan_mod._validate_tei_embedding_model(
                _svc("http://tei.local:8080")
            )


@pytest.mark.asyncio
async def test_tei_non_200_skips_without_raise():
    p_client, _ = _client_patch(_resp(404))
    with patch.object(lifespan_mod, "settings") as s, patch.object(
        lifespan_mod, "assert_url_safe"
    ), p_client:
        s.embedding_model = "anything"
        # 404 on /info -> skip validation, must not raise.
        await lifespan_mod._validate_tei_embedding_model(
            _svc("http://tei.local:8080")
        )


@pytest.mark.asyncio
async def test_tei_urlblocked_skips_without_probing():
    p_client, client = _client_patch(_resp(200, {"model_id": "x"}))
    with patch.object(lifespan_mod, "settings") as s, patch.object(
        lifespan_mod, "assert_url_safe", side_effect=URLBlocked("blocked")
    ), p_client:
        s.embedding_model = "anything"
        # SSRF guard blocks the host -> skip validation, no GET, no raise.
        await lifespan_mod._validate_tei_embedding_model(
            _svc("http://tei.local:8080")
        )

    client.get.assert_not_called()
