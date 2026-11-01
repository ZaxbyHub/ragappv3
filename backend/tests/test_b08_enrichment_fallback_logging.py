"""Issue #697 acceptance checks (Workstream B PR 8) — fallback logging.

AC6 (test_vault_helper_db_error_is_logged): when the DB layer raises
under ``is_enrichment_enabled_for_vault`` (``get_pool`` patched with
``side_effect=RuntimeError``), the silent except-fallback must emit a
WARNING+ log record on the ``app.services.document_processor`` logger
instead of swallowing the error invisibly.

AC7 (test_file_helper_db_error_is_logged): the same contract for
``is_enrichment_enabled_for_file``.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

_B08_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}


@pytest.fixture(autouse=True, scope="module")
def _b08_hermetic_env():
    """Set hermetic env BEFORE any app import; restore afterwards.

    Mirrors backend/tests/conftest.py and the C9 harness's ``_hermetic_env``
    pattern: every app import in this module is function-local, so this
    module-scoped fixture has run (and the env keys are set) before the
    first ``app.*`` module is imported. All previous values are restored on
    teardown so later modules see the conftest-provided environment.
    """
    saved = {key: os.environ.get(key) for key in _B08_ENV}
    os.environ.update(_B08_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_vault_helper_db_error_is_logged(caplog) -> None:
    """AC6 (issue #697): the vault helper logs its DB-error fallback."""
    from app.services import document_processor

    with caplog.at_level(
        logging.WARNING, logger="app.services.document_processor"
    ), patch.object(
        document_processor, "get_pool", side_effect=RuntimeError("b08 ac6 probe")
    ):
        document_processor.is_enrichment_enabled_for_vault(1)
    n = len([r for r in caplog.records if r.levelno >= logging.WARNING])
    assert n >= 1


def test_file_helper_db_error_is_logged(caplog) -> None:
    """AC7 (issue #697): the file helper logs its DB-error fallback."""
    from app.services import document_processor

    with caplog.at_level(
        logging.WARNING, logger="app.services.document_processor"
    ), patch.object(
        document_processor, "get_pool", side_effect=RuntimeError("b08 ac7 probe")
    ):
        document_processor.is_enrichment_enabled_for_file(1, 1)
    n = len([r for r in caplog.records if r.levelno >= logging.WARNING])
    assert n >= 1
