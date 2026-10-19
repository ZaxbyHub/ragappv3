"""
C15 provider-body redaction tests for the embedding service (issue #903).

The embedding provider's raw HTTP error body (``response.text``) used to be
formatted verbatim into WARNING-level log records on nine failure paths.
Provider error bodies are third-party content that can echo the request
payload back — token-overflow errors in particular quote the offending input
text — and embedding inputs are user-derived document/chunk/query content,
so the raw body must never reach the default (WARNING+) application log
(C15 class contract from #287/#710: no raw user-derived text at default log
level).

Contract pinned here:
- No WARNING-or-higher record from ``app.services.embeddings`` contains
  provider body text (discriminated with a distinct ``C15ET_MARKER`` marker
  that also simulates a provider echoing the embedded input).
- Failures are still logged at WARNING with the status code, the provider
  mode, and a bounded non-echoing body summary (length + SHA-256
  fingerprint), and the raised ``EmbeddingError`` message keeps carrying the
  status code (AC2).
- The token-overflow recovery path keeps working (split + mean-pool
  succeeds) without leaking the body (AC3).

The three test functions are module-level with the exact names the #903
acceptance criteria reference, so the AC argv node IDs resolve verbatim.
"""
import logging
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

from unittest.mock import MagicMock, patch

import pytest

from app.services.embeddings import EmbeddingError, EmbeddingService

# Distinct marker a stubbed provider error body carries; if any WARNING+
# record contains it, raw provider body text reached the default log.
C15ET_MARKER = "C15ET_MARKER"

# A second marker embedded in the INPUT text itself (not just the body):
# simulates a provider error body that echoes the offending input back —
# the strongest disclosure risk (token-overflow bodies quote the input).
INPUT_ECHO_SENTINEL = "CONFIDENTIAL-INPUT-SENTINEL"

# Status-line-style body used on generic non-200 paths.
BAD_REQUEST_BODY = (
    f"400 Bad Request: {C15ET_MARKER} provider echoed input fragment here"
)

# Body that matches the token-overflow sniff ("input (" + "tokens) is too
# large" via _is_token_overflow_error pattern 1) while carrying both markers.
OVERFLOW_BODY_TEMPLATE = (
    "input (4096 tokens) is too large to process; reduce batch size. "
    f"{C15ET_MARKER} provider echo: {{input_echo}}"
)

# embed_batch overflow trigger: texts longer than this get the 500 overflow
# response from the stub; shorter texts get a valid embedding.
OVERFLOW_TRIGGER_CHARS = 2500

EMBED_DIM = 768


@pytest.fixture(autouse=True)
def mock_settings():
    """Mock settings for all tests (same shape as test_embeddings.py)."""
    with patch('app.services.embeddings.settings') as mock_settings, \
         patch('app.services.embeddings.assert_url_safe'):
        # Modern /api/embed route: adaptive batching speaks the batched-input
        # dialect (EMBED-001), which is what the batch paths below drive.
        mock_settings.ollama_embedding_url = "http://localhost:11434/api/embed"
        mock_settings.embedding_model = "nomic-embed-text"
        mock_settings.embedding_doc_prefix = ""
        mock_settings.embedding_query_prefix = ""
        mock_settings.embedding_batch_size = 512
        mock_settings.embedding_batch_max_retries = 3
        mock_settings.embedding_batch_min_sub_size = 1
        mock_settings.embedding_concurrent_batches = 4
        mock_settings.embedding_batch_max_chars = 131072
        mock_settings.chunk_size_chars = 1200
        mock_settings.chunk_overlap_chars = 120
        yield mock_settings


def _response(status_code: int, text: str, json_value=None, json_error=None):
    """Mock httpx.Response with the given status, raw body, and JSON outcome."""
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = json_value
    return response


def _service_with_response(response) -> EmbeddingService:
    """Service whose HTTP client always returns the given response."""
    service = EmbeddingService()
    mock_client = MagicMock()

    async def mock_post(url, json=None, **kwargs):
        return response

    mock_client.post = mock_post
    service._client = mock_client
    return service


def _service_with_overflow_on_long_inputs() -> EmbeddingService:
    """Service that 500s with a marker-bearing overflow body for long inputs.

    The 500 body matches the token-overflow sniff AND carries both markers:
    the body marker and an echo of the first 80 chars of the offending input
    (what real token-overflow errors do). Shorter inputs embed normally.
    """
    service = EmbeddingService()
    mock_client = MagicMock()

    async def mock_post(url, json=None, **kwargs):
        inputs = (json or {}).get("input") or []
        if len(inputs) == 1 and len(inputs[0]) > OVERFLOW_TRIGGER_CHARS:
            body = OVERFLOW_BODY_TEMPLATE.format(
                input_echo=f"{inputs[0][:80]}...{INPUT_ECHO_SENTINEL}"
            )
            return _response(500, body)
        return _response(
            200, "", json_value={"embeddings": [[0.1] * EMBED_DIM] * len(inputs)}
        )

    mock_client.post = mock_post
    service._client = mock_client
    return service


def _marker_count(records) -> int:
    """Number of captured records whose rendered message carries the marker."""
    return sum(
        1 for record in records if C15ET_MARKER in record.getMessage()
    )


def _sentinel_count(records) -> int:
    """Number of captured records whose message carries the input-echo sentinel."""
    return sum(
        1 for record in records if INPUT_ECHO_SENTINEL in record.getMessage()
    )


@pytest.mark.asyncio
async def test_error_body_marker_absent_from_warning_log(caplog):
    """AC1: on every non-200 / invalid-JSON / outage-status failure path
    (single, probe, and batch), no WARNING-or-higher record from
    app.services.embeddings contains the provider body marker."""
    # (label, failure factory) — every drive raises EmbeddingError and
    # exercises one of the nine response.text WARNING sites.
    drives = [
        (
            "embed_single non-200",
            _service_with_response(_response(400, BAD_REQUEST_BODY)),
            lambda s: s.embed_single("user query text"),
        ),
        (
            "embed_single outage-status",
            _service_with_response(
                _response(503, f"503 Service Unavailable: {C15ET_MARKER} body")
            ),
            lambda s: s.embed_single("user query text"),
        ),
        (
            "embed_single invalid JSON",
            _service_with_response(
                _response(
                    200,
                    f"<html>{C15ET_MARKER} gateway noise</html>",
                    json_error=ValueError("invalid json"),
                )
            ),
            lambda s: s.embed_single("user query text"),
        ),
        (
            "embed_probe non-200",
            _service_with_response(_response(400, BAD_REQUEST_BODY)),
            lambda s: s.embed_probe(5.0),
        ),
        (
            "embed_probe outage-status",
            _service_with_response(
                _response(503, f"503 Service Unavailable: {C15ET_MARKER} body")
            ),
            lambda s: s.embed_probe(5.0),
        ),
        (
            "embed_probe invalid JSON",
            _service_with_response(
                _response(
                    200,
                    f"<html>{C15ET_MARKER} gateway noise</html>",
                    json_error=ValueError("invalid json"),
                )
            ),
            lambda s: s.embed_probe(5.0),
        ),
        (
            "embed_batch non-200",
            _service_with_response(_response(400, BAD_REQUEST_BODY)),
            lambda s: s.embed_batch(["batch text one"]),
        ),
        (
            "embed_batch outage-status",
            _service_with_response(
                _response(503, f"503 Service Unavailable: {C15ET_MARKER} body")
            ),
            lambda s: s.embed_batch(["batch text one"]),
        ),
    ]

    captured = []
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        for label, service, drive in drives:
            before = len(caplog.records)
            with pytest.raises(EmbeddingError):
                await drive(service)
            # Non-vacuous: each failure path must still log at WARNING+.
            assert len(caplog.records) > before, (
                f"expected at least one WARNING+ record for drive: {label}"
            )
            captured.extend(caplog.records[before:])

    assert captured, "test must capture WARNING+ records to be meaningful"
    assert _marker_count(captured) == 0, (
        "provider error body text reached the default log: "
        f"{[r.getMessage() for r in captured if C15ET_MARKER in r.getMessage()]}"
    )


@pytest.mark.asyncio
async def test_failure_still_logged_with_status_and_mode(caplog):
    """AC2: redaction must not silence the operational signal — WARNING
    records keep the status code, the provider mode, and a bounded body
    summary, and the raised EmbeddingError keeps the status code."""
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        # Single-text failure path.
        service = _service_with_response(_response(400, BAD_REQUEST_BODY))
        with pytest.raises(EmbeddingError) as single_exc:
            await service.embed_single("user query text")

        single_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "status 400" in r.getMessage()
            and "ollama mode" in r.getMessage()
        ]
        assert single_warnings, (
            "embed_single failure must log WARNING with status and mode"
        )

        # Health-probe failure path.
        probe_service = _service_with_response(_response(400, BAD_REQUEST_BODY))
        with pytest.raises(EmbeddingError):
            await probe_service.embed_probe(5.0)

    probe_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "status 400" in r.getMessage()
        and "ollama mode" in r.getMessage()
    ]
    assert probe_warnings, (
        "embed_probe failure must log WARNING with status and mode"
    )

    for record in single_warnings + probe_warnings:
        message = record.getMessage()
        assert "body_length=" in message and "body_sha256=" in message, (
            f"WARNING lacks the bounded provider-body summary: {message}"
        )
        assert C15ET_MARKER not in message

    assert "status 400" in str(single_exc.value), (
        "raised EmbeddingError must keep carrying the status code"
    )


@pytest.mark.asyncio
async def test_token_overflow_body_not_logged(caplog):
    """AC3: the token-overflow path (strongest echo risk) emits no WARNING+
    record containing the provider body text, and overflow recovery still
    completes."""
    long_text = (
        f"{INPUT_ECHO_SENTINEL} user document chunk content. "
        + "embedding input padding sentence. " * 110
    )
    assert len(long_text) > OVERFLOW_TRIGGER_CHARS

    service = _service_with_overflow_on_long_inputs()
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        embeddings = await service.embed_batch([long_text])

    assert len(embeddings) == 1, "overflow recovery must still succeed"
    assert len(embeddings[0]) == EMBED_DIM
    warning_records = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert _marker_count(warning_records) == 0, (
        "token-overflow provider body leaked into the default log: "
        f"{[r.getMessage() for r in warning_records]}"
    )
    assert _sentinel_count(warning_records) == 0, (
        "provider-echoed input text leaked into the default log: "
        f"{[r.getMessage() for r in warning_records]}"
    )
