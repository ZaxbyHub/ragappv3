"""Acceptance checks for issue #903 (C15): embedding provider error bodies.

The #287/#710 logging contract ("no raw user-derived text at default log
level") reaches the embedding provider surface: an HTTP error body can echo
the request payload back (token-overflow bodies in particular quote the
offending input segment), and ``backend/app/services/embeddings.py`` formats
``response.text`` verbatim into WARNING f-strings on nine failure paths.

- AC1 (C1): a provider error body carrying a distinct marker must never
  appear in a WARNING-or-higher record of ``app.services.embeddings``.
  RED at base, GREEN after the fix.
- AC2 (C2): the failure WARNING must keep the status code and the mode (plus
  whatever bounded summary the fix chooses), and the raised ``EmbeddingError``
  message must keep the status code. GREEN at base, GREEN after the fix.
- AC3 (C3): the token-overflow branch (the strongest echo risk) must emit no
  WARNING-or-higher record containing the provider body text. RED at base,
  GREEN after the fix.

Marker-probe style per the #904 review lesson: the marker rides the
user-derived input, a manually attached capture handler collects records
(assertLogs would fail a passing fix by design when zero records exist), and
the absence assertion counts marker-bearing INFO+ records.
"""

import logging
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from app.services.embeddings import EmbeddingError, EmbeddingService

EMBEDDINGS_LOGGER = "app.services.embeddings"


class _ListHandler(logging.Handler):
    """Collect every record at or above its level; no assertion semantics."""

    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    """Capture records of ``logger_name`` at ``level``+ without failing when
    the logger stays silent (unlike ``assertLogs``)."""
    lg = logging.getLogger(logger_name)
    handler = _ListHandler(level)
    previous_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)


@pytest.fixture
def emb_settings():
    """Patch settings the same way tests/test_embeddings.py does (modern
    /api/embed dialect so embed_batch funnels straight into the retry loop)."""
    with patch("app.services.embeddings.settings") as mock_settings, patch(
        "app.services.embeddings.assert_url_safe"
    ):
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
        mock_settings.chunk_overlap_chars = 128
        yield mock_settings


def _error_response(status_code: int, body: str) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = body
    return response


async def test_error_body_marker_absent_from_warning_log(emb_settings):
    """AC1: provider error body marker absent from all WARNING+ records.

    A 400 whose body echoes the embedded input (carrying C15ET_MARKER) drives
    ``embed_single`` through the non-outage WARNING site. RED at base (the
    WARNING formats ``response.text`` verbatim), GREEN after the fix.
    """
    service = EmbeddingService()
    # PRR-017d: __init__ builds a real httpx.AsyncClient (SSRFSafeTransport);
    # these tests swap in a stub, so close the original instead of leaking it.
    _real_client = service._client
    marker_body = (
        "invalid request: input token limit exceeded near C15ET_MARKER "
        "secret user-derived segment quoted by the provider"
    )
    await _real_client.aclose()
    service._client = MagicMock()
    service._client.post = AsyncMock(
        return_value=_error_response(400, marker_body)
    )

    with capture_records(EMBEDDINGS_LOGGER, logging.WARNING) as captured:
        with pytest.raises(EmbeddingError):
            await service.embed_single("user derived operand text")

    # Positive pairing (swarm-pr-review PRR-017a): pytest.raises can be
    # satisfied by any of the module's ~44 raise sites, so an absence-only check
    # could pass with ZERO captured records. Pin the failure record first.
    assert any(
        "Embedding API returned status 400" in r.getMessage()
        for r in captured.records
    ), (
        "the 400 response must log its own WARNING+ record; captured: "
        f"{[r.getMessage() for r in captured.records]}"
    )
    leaks = [r for r in captured.records if "C15ET_MARKER" in r.getMessage()]
    assert leaks == [], (
        f"{len(leaks)} WARNING+ record(s) of {EMBEDDINGS_LOGGER} carry the "
        f"provider error body verbatim (violates the #287/#710/#903 no-echo "
        f"contract): {[r.getMessage() for r in leaks]}"
    )


async def test_failure_still_logged_with_status_and_mode(emb_settings):
    """AC2: the failure WARNING keeps status + mode; EmbeddingError keeps status.

    GREEN at base, GREEN after the fix — redaction must not silence the
    operational signal (outage detection keys on the status code).
    """
    service = EmbeddingService()
    # PRR-017d: __init__ builds a real httpx.AsyncClient (SSRFSafeTransport);
    # these tests swap in a stub, so close the original instead of leaking it.
    _real_client = service._client
    await _real_client.aclose()
    service._client = MagicMock()
    service._client.post = AsyncMock(
        return_value=_error_response(400, "bad request body C15ET_MARKER")
    )

    with capture_records(EMBEDDINGS_LOGGER, logging.WARNING) as captured:
        with pytest.raises(EmbeddingError) as excinfo:
            await service.embed_single("user derived operand text")

    failure_warnings = [
        r.getMessage() for r in captured.records if r.levelno >= logging.WARNING
    ]
    assert any(
        "400" in message and "ollama" in message for message in failure_warnings
    ), (
        "an embedding HTTP failure must still log a WARNING carrying the "
        f"status code and the mode; captured: {failure_warnings}"
    )
    assert "400" in str(excinfo.value), (
        "the raised EmbeddingError message must keep the HTTP status code, "
        f"got: {excinfo.value!r}"
    )


async def test_token_overflow_body_not_logged(emb_settings):
    """AC3: token-overflow branch emits no WARNING+ record with the body.

    The body matches the existing ``token limit exceeded`` sniff, so the
    500-response overflow branch fires and (at base) logs ``response.text``
    verbatim before the overflow-retry helper takes over. The single short
    input bounds the retry (it fails fast below MIN_SPLIT_CHARS). RED at
    base, GREEN after the fix.
    """
    service = EmbeddingService()
    # PRR-017d: __init__ builds a real httpx.AsyncClient (SSRFSafeTransport);
    # these tests swap in a stub, so close the original instead of leaking it.
    _real_client = service._client
    marker_body = (
        "input token limit exceeded: provider quotes the offending segment "
        "C15ET_MARKER user text verbatim"
    )
    await _real_client.aclose()
    service._client = MagicMock()
    service._client.post = AsyncMock(
        return_value=_error_response(500, marker_body)
    )

    with capture_records(EMBEDDINGS_LOGGER, logging.WARNING) as captured:
        with pytest.raises(EmbeddingError):
            await service.embed_batch(["short operand under split floor"], batch_size=1)

    # Positive pairing (PRR-017a): pytest.raises can be satisfied by any of the
    # module's ~44 raise sites, so pin the overflow record before absence.
    assert any(
        "Token overflow error" in r.getMessage() for r in captured.records
    ), (
        "the token-overflow request must log its own WARNING+ record; captured: "
        f"{[r.getMessage() for r in captured.records]}"
    )
    leaks = [r for r in captured.records if "C15ET_MARKER" in r.getMessage()]
    assert leaks == [], (
        f"{len(leaks)} WARNING+ record(s) of {EMBEDDINGS_LOGGER} carry the "
        f"token-overflow provider body verbatim (the strongest #903 echo "
        f"risk): {[r.getMessage() for r in leaks]}"
    )


# ---------------------------------------------------------------------------
# Review follow-up (swarm-pr-review PRR-005, PR #981): the tenth site.
#
# The recurrence census enumerated the literal ``response.text`` attribute and
# so missed the token-overflow branch of ``_embed_batch_with_retry``, which
# reaches the same provider body through the ``resp`` alias. The pre-existing
# HTTPError tests in tests/test_embeddings.py raise WITHOUT a ``.response``
# (``exc.response`` is only populated for HTTPStatusError), so they cannot see
# this site at all. The pin below constructs the shape that does.
# ---------------------------------------------------------------------------

# LOWERCASE on purpose: the branch logs ``response_text = resp.text.lower()``,
# so an uppercase marker could never match a leaked line and the leak
# assertion would be structurally dead (swarm-pr-feedback critic round).
BODY_MARKER_BATCH = "c15batchbodymarker"


def _status_error_with_body(body: str) -> httpx.HTTPStatusError:
    """A transport error that DOES carry a response with a body."""
    request = httpx.Request("POST", "http://embeddings.invalid/v1/embeddings")
    response = httpx.Response(
        500,
        request=request,
        content=body.encode("utf-8"),
        headers={"content-type": "text/plain"},
    )
    return httpx.HTTPStatusError("server error", request=request, response=response)


async def test_batch_httpx_error_branch_does_not_log_provider_body(emb_settings):
    """The HTTPError overflow branch logs ``body_len=``, never the body.

    RED before the PRR-005 fix (the branch formatted the whole lowercased body
    into the WARNING), GREEN after. Also pins that overflow DETECTION still
    reads the full body — only the log line is truncated.
    """
    service = EmbeddingService()
    _real_client = service._client
    try:
        service._client = MagicMock()
        service._client.post = AsyncMock(
            side_effect=_status_error_with_body(
                f"token limit exceeded: {BODY_MARKER_BATCH} quoted provider text"
            )
        )

        with capture_records(EMBEDDINGS_LOGGER, logging.WARNING) as captured:
            with pytest.raises(Exception):
                await service.embed_batch(["user derived operand text"])

        warnings = [r for r in captured.records if r.levelno >= logging.WARNING]
        leaks = [r for r in warnings if BODY_MARKER_BATCH in r.getMessage()]
        assert leaks == [], (
            "the batch HTTPError branch leaked the provider body at WARNING+: "
            f"{[r.getMessage() for r in leaks]}"
        )
        overflow_lines = [
            r.getMessage()
            for r in warnings
            if "Token overflow error" in r.getMessage()
        ]
        assert overflow_lines, (
            "the overflow branch must still fire (detection reads the full "
            f"body); captured: {[r.getMessage() for r in warnings]}"
        )
        assert "body_len=" in overflow_lines[0], overflow_lines[0]
    finally:
        await _real_client.aclose()
