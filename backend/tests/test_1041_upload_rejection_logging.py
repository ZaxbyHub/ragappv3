"""Issue #1041: upload 400 rejections must log one detail line server-side.

Three frozen checks (AC1), one per validation path in ``_do_upload`` — no
file, empty filename, disallowed extension. Each rejection must emit
EXACTLY one WARNING record from the documents logger carrying the
rejection reason, the submitted filename (where present), the vault id,
and the requesting user id, while the HTTPException status/detail stay
byte-identical to the pre-fix contract (AC2).

AC2/AC3 structural guards: the only new log site is
``_log_upload_rejection`` — called exactly three times in
``documents.py``, all inside ``_do_upload`` (no per-success lines added),
and its arguments are metadata only (filename never file contents; the
``#287``/#730 default-log redaction contract enforced separately by
``test_no_query_text_in_default_logs.py``, which stays untouched).
"""

import ast
import io
import logging
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
os.environ.setdefault("USERS_ENABLED", "false")

from app.api.routes import documents as documents_module
from app.api.routes.documents import _do_upload
from app.config import settings

VAULT_ID = 7
USER = {"id": 4242}
CONTENT_MARKER = "TOP-SECRET-FILE-CONTENTS-1041"

documents_logger_name = documents_module.logger.name


def _rejection_records(caplog):
    return [r for r in caplog.records if r.name == documents_logger_name]


def _assert_single_warning_line(caplog):
    records = _rejection_records(caplog)
    assert len(records) == 1, f"expected exactly one log line, got {records!r}"
    record = records[0]
    assert record.levelno == logging.WARNING
    return record.getMessage()


async def test_no_file_rejection_logs_one_line(caplog):
    request = SimpleNamespace(headers={})
    with caplog.at_level(logging.WARNING, logger=documents_logger_name):
        with pytest.raises(HTTPException) as exc_info:
            await _do_upload(
                request, None, settings, None, None, None, None, VAULT_ID, USER
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "No file provided"

    message = _assert_single_warning_line(caplog)
    assert "No file provided" in message
    assert f"vault_id={VAULT_ID}" in message
    assert "user_id=4242" in message


async def test_empty_filename_rejection_logs_one_line(caplog):
    request = SimpleNamespace(headers={})
    file = SimpleNamespace(filename="", file=io.BytesIO(CONTENT_MARKER.encode()))
    with caplog.at_level(logging.WARNING, logger=documents_logger_name):
        with pytest.raises(HTTPException) as exc_info:
            await _do_upload(
                request, file, settings, None, None, None, None, VAULT_ID, USER
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Filename cannot be empty"

    message = _assert_single_warning_line(caplog)
    assert "Filename cannot be empty" in message
    assert f"vault_id={VAULT_ID}" in message
    assert "user_id=4242" in message
    # AC3: filenames/metadata only — never file contents.
    assert CONTENT_MARKER not in message


async def test_disallowed_extension_rejection_logs_one_line(caplog, tmp_path):
    original_data_dir = settings.data_dir
    original_allowed = settings.allowed_extensions
    settings.data_dir = tmp_path
    settings.allowed_extensions = {".txt"}
    request = SimpleNamespace(headers={})
    # Content stream is present but must never be read into a log line.
    file = SimpleNamespace(
        filename="installer.exe", file=io.BytesIO(CONTENT_MARKER.encode())
    )
    try:
        with caplog.at_level(logging.WARNING, logger=documents_logger_name):
            with pytest.raises(HTTPException) as exc_info:
                await _do_upload(
                    request, file, settings, None, None, None, None, VAULT_ID, USER
                )
    finally:
        settings.data_dir = original_data_dir
        settings.allowed_extensions = original_allowed

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "File extension '.exe' not allowed. Allowed: {'.txt'}"
    )

    message = _assert_single_warning_line(caplog)
    assert "File extension '.exe' not allowed" in message
    assert "installer.exe" in message
    assert f"vault_id={VAULT_ID}" in message
    assert "user_id=4242" in message
    # AC3: filenames/metadata only — never file contents.
    assert CONTENT_MARKER not in message


def test_rejection_log_helper_called_only_at_three_rejection_sites():
    """AC2 structural guard: the only new log site is the rejection helper.

    Parses documents.py and asserts ``_log_upload_rejection`` is called
    exactly three times, every call lives inside ``_do_upload`` (so no
    per-success path gained a log line), and each call passes exactly the
    four metadata arguments (reason, filename, vault_id, user) — never a
    file-content expression such as ``file.file``.
    """
    source = open(documents_module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)

    do_upload = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_do_upload"
    )

    calls = [
        node
        for node in ast.walk(do_upload)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_log_upload_rejection"
    ]
    module_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_log_upload_rejection"
    ]
    assert len(module_calls) == 3
    assert len(calls) == 3  # all three live inside _do_upload

    for call in calls:
        assert len(call.args) == 4, ast.dump(call)
        assert not any(
            isinstance(arg, ast.Attribute) and arg.attr == "file"
            for arg in ast.walk(call.args[1])  # filename slot
        ), "filename slot must carry the filename, never the content stream"
