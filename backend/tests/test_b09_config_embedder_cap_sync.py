"""Issue #698 (Workstream B PR 9) — config/embedder cap drift guard.

`EMBEDDING_MAX_TEXT_CHARS` (config.py) and `EmbeddingService.MAX_TEXT_LENGTH`
are intentionally separate because config cannot import the service
(circular import); this guard pins their equality so the mirrored constant
cannot drift. It exists as its own (non-frozen) file so feedback rounds can
extend it without touching frozen manifest paths.
"""

from __future__ import annotations

import os

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


def test_config_cap_equals_embedder_cap():
    from app.config import EMBEDDING_MAX_TEXT_CHARS
    from app.services.embeddings import EmbeddingService

    assert EMBEDDING_MAX_TEXT_CHARS == EmbeddingService.MAX_TEXT_LENGTH


def test_chunk_size_family_validators_use_the_cap():
    """The chunk-size validators reference the shared cap, not a bare literal."""
    import inspect

    import app.config as config_module

    src = inspect.getsource(
        config_module.Settings.validate_chunk_size_chars_bounds
    )
    assert "EMBEDDING_MAX_TEXT_CHARS" in src
    multi_src = inspect.getsource(
        config_module.Settings.validate_multi_scale_chunk_sizes
    )
    assert "EMBEDDING_MAX_TEXT_CHARS" in multi_src
