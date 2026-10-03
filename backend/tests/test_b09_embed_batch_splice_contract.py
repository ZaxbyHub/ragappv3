"""Issue #698 (Workstream B PR 9) — embed_batch fail_fast=False splice contract.

Pins the per-text degradation contract the frozen check C3 cannot observe
(C3 asserts only "no raise"): with fail_fast=False an invalid text (None /
whitespace / oversized) is skipped without a provider call, receives a None
placeholder at its ORIGINAL position, and is reported in one WARNING; valid
texts keep their relative order and batch semantics; the count invariant
len(embeddings) == len(texts) holds for empty, all-invalid, and mixed inputs.
Consumers couple positionally (document_processor.py zip(chunks, embeddings);
background_tasks emb_list[i]), so a mis-splice is a silent wrong-vector bug.

Also pins the AC5 PUT-path bounds: direct SettingsUpdate values and values
DERIVED from legacy chunk_size PUTs (x4, after apply_legacy_settings_conversion)
both 422 above the embedder per-text cap or at/below zero.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

_B09_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}


@pytest.fixture(autouse=True, scope="module")
def _b09_hermetic_env():
    """Hermetic env before any app import; restored on teardown."""
    saved = {key: os.environ.get(key) for key in _B09_ENV}
    os.environ.update(_B09_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _make_service():
    """Build an EmbeddingService with patched settings/SSRF (test idiom)."""
    from app.services.embeddings import EmbeddingService

    settings_patcher = patch("app.services.embeddings.settings")
    ssrf_patcher = patch("app.services.embeddings.assert_url_safe")
    mock_settings = settings_patcher.start()
    ssrf_patcher.start()
    mock_settings.ollama_embedding_url = "http://localhost:11434/api/embed"
    mock_settings.embedding_model = "nomic-embed-text"
    mock_settings.embedding_doc_prefix = ""
    mock_settings.embedding_query_prefix = ""
    mock_settings.embedding_batch_size = 512
    mock_settings.embedding_batch_max_retries = 3
    mock_settings.embedding_batch_min_sub_size = 1
    mock_settings.embedding_concurrent_batches = 4
    mock_settings.embedding_batch_max_chars = 131072

    service = EmbeddingService()
    service._patchers = (settings_patcher, ssrf_patcher)  # type: ignore[attr-defined]
    return service


@pytest.fixture
def service():
    svc = _make_service()
    yield svc
    for patcher in svc._patchers:  # type: ignore[attr-defined]
        patcher.stop()


def _ok_client(sent_batches):
    """A mock HTTP client recording each batch payload and returning 768-dim vectors."""
    client = MagicMock()

    async def mock_post(url, json=None, **kwargs):
        inputs = (json or {}).get("input", [])
        sent_batches.append(list(inputs))
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"embeddings": [[0.1] * 768] * len(inputs)}
        return response

    client.post = mock_post
    return client


async def test_mixed_input_splices_none_at_original_positions(service):
    """Oversized and whitespace texts get None; valid texts get real vectors."""
    sent_batches: list[list[str]] = []
    service._client = _ok_client(sent_batches)

    texts = ["x" * 9000, "short", "   "]
    embeddings, failed = await service.embed_batch(texts, fail_fast=False)

    assert len(embeddings) == len(texts)
    assert embeddings[0] is None
    assert embeddings[2] is None
    assert embeddings[1] == [0.1] * 768
    assert failed == []
    # The invalid texts never reached the provider.
    assert sent_batches == [["short"]]


async def test_all_invalid_input_returns_all_none_without_provider_call(service):
    """Every text invalid: all-None result, no provider calls, no failed batches."""
    sent_batches: list[list[str]] = []
    service._client = _ok_client(sent_batches)

    embeddings, failed = await service.embed_batch(
        ["x" * 9000, "  "], fail_fast=False
    )

    assert embeddings == [None, None]
    assert failed == []
    assert sent_batches == []


async def test_empty_input_early_return_shape_preserved(service):
    """Empty input keeps the documented ([], []) return shape."""
    result = await service.embed_batch([], fail_fast=False)
    assert result == ([], [])


async def test_provider_failure_reports_batch_and_none_placeholders(service):
    """A provider-failed batch lands in failed_batch_indices with None placeholders."""
    import app.services.embeddings as emb_module

    # Force each text into its own char-bounded batch so one text's provider
    # failure does not take its sibling's batch down with it.
    emb_module.settings.embedding_batch_max_chars = 20
    client = MagicMock()

    async def mock_post(url, json=None, **kwargs):
        inputs = (json or {}).get("input", [])
        if any(t.startswith("boom") for t in inputs):
            raise RuntimeError("provider down")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"embeddings": [[0.1] * 768] * len(inputs)}
        return response

    client.post = mock_post
    service._client = client

    texts = ["boom-trigger", "fine text"]
    embeddings, failed = await service.embed_batch(texts, fail_fast=False)

    assert len(embeddings) == len(texts)
    assert embeddings[0] is None
    assert embeddings[1] == [0.1] * 768
    assert failed != []


async def test_fail_fast_true_still_raises_on_first_invalid(service):
    """Default fail_fast=True keeps the exact raise contract (message shapes)."""
    service._client = _ok_client([])
    from app.services.embeddings import EmbeddingError

    with pytest.raises(EmbeddingError, match="exceeds maximum length"):
        await service.embed_batch(["x" * 9000, "short"])

    with pytest.raises(EmbeddingError, match="empty or whitespace only"):
        await service.embed_batch(["ok", "   "])


async def test_invalid_logged_as_warning(service, caplog):
    """Invalid positions are reported through one WARNING."""
    import logging

    sent_batches: list[list[str]] = []
    service._client = _ok_client(sent_batches)
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        await service.embed_batch(["x" * 9000, "short"], fail_fast=False)
    joined = "\n".join(rec.message for rec in caplog.records)
    assert "position(s) [0]" in joined


# ── AC5 PUT-path bounds (direct + legacy-derived) ──────────────────────────


def test_settings_update_direct_above_bound_rejected():
    """Direct PUT value above the cap fails field validation."""
    from pydantic import ValidationError

    from app.api.routes.settings import SettingsUpdate

    with pytest.raises(ValidationError):
        SettingsUpdate(chunk_size_chars=9000)


def test_settings_update_direct_non_positive_rejected():
    """Direct PUT value at/below zero fails field validation."""
    from pydantic import ValidationError

    from app.api.routes.settings import SettingsUpdate

    with pytest.raises(ValidationError):
        SettingsUpdate(chunk_size_chars=0)
    with pytest.raises(ValidationError):
        SettingsUpdate(chunk_size_chars=-4)


def test_legacy_put_derived_above_bound_returns_422():
    """Legacy chunk_size PUT deriving an out-of-bound value 422s post-conversion."""
    from fastapi import HTTPException

    from app.api.routes.settings import SettingsUpdate, _validate_settings_update

    with pytest.raises(HTTPException) as exc_info:
        _validate_settings_update(SettingsUpdate(chunk_size=3000))
    assert exc_info.value.status_code == 422
    assert "8192" in str(exc_info.value.detail)


def test_legacy_put_non_positive_rejected_at_field_level():
    """Legacy chunk_size=0 is rejected by the legacy field's own validator.

    (The derived ≤0 hole is therefore unreachable via the PUT path; the
    post-conversion both-bounds predicate in _validate_settings_update is
    defense-in-depth for any future converter change.)
    """
    from pydantic import ValidationError

    from app.api.routes.settings import SettingsUpdate

    with pytest.raises(ValidationError):
        SettingsUpdate(chunk_size=0)
    with pytest.raises(ValidationError):
        SettingsUpdate(chunk_size=-1)


def test_legacy_put_in_range_still_converts():
    """In-range legacy PUT keeps converting (no regression of issue #494)."""
    from app.api.routes.settings import SettingsUpdate, _validate_settings_update

    converted = _validate_settings_update(SettingsUpdate(chunk_size=1000))
    assert converted["chunk_size_chars"] == 4000


def test_settings_construction_both_bounds():
    """Settings construction rejects above-cap and non-positive values."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(chunk_size_chars=9000)
    with pytest.raises(ValidationError):
        Settings(chunk_size_chars=0)


def test_settings_construction_overlap_must_stay_below_size():
    """Cross-field: overlap >= size (direct or legacy-derived) is rejected.

    The chunker (unstructured 0.18.32) rejects overlap >= size with
    "'overlap' argument must be less than max_characters" — without this
    validator such a config fails EVERY ingest instead of failing at
    configuration time (issue #698 review finding).
    """
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="strictly less"):
        Settings(chunk_size_chars=1000, chunk_overlap_chars=5000)
    with pytest.raises(ValidationError, match="strictly less"):
        Settings(chunk_size_chars=1000, chunk_overlap_chars=1000)
    # Legacy-derived pair: chunk_size=100/chunk_overlap=100 -> 400/400.
    with pytest.raises(ValidationError, match="strictly less"):
        Settings(chunk_size=100, chunk_overlap=100)
    # In-range pair still constructs.
    assert Settings(chunk_size_chars=1000, chunk_overlap_chars=200) is not None


def test_multi_scale_overlap_ratio_one_rejected():
    """ratio=1.0 makes per-scale overlap == scale and kills every ingest."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="multi_scale_overlap_ratio"):
        Settings(multi_scale_overlap_ratio=1.0)
    # The rest of the range stays valid.
    assert Settings(multi_scale_overlap_ratio=0.99) is not None
    assert Settings(multi_scale_overlap_ratio=0.0) is not None


def test_put_cross_field_overlap_above_resulting_size_422():
    """PUT overlap >= resulting size 422s, including single-sided updates."""
    from fastapi import HTTPException

    from app.api.routes.settings import SettingsUpdate, _validate_settings_update

    with pytest.raises(HTTPException) as exc_info:
        _validate_settings_update(
            SettingsUpdate(chunk_size_chars=1000, chunk_overlap_chars=5000)
        )
    assert exc_info.value.status_code == 422
    # Single-sided: only overlap provided, compared against the current size.
    with pytest.raises(HTTPException) as exc_info:
        _validate_settings_update(SettingsUpdate(chunk_overlap_chars=1_000_000))
    assert exc_info.value.status_code == 422
    # In-range pair still converts cleanly.
    converted = _validate_settings_update(
        SettingsUpdate(chunk_size_chars=1000, chunk_overlap_chars=200)
    )
    assert converted["chunk_size_chars"] == 1000
