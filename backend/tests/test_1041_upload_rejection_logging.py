"""Issue #1041: upload 400 rejections must log one detail line server-side.

Three frozen checks (AC1), one per validation path in ``_do_upload`` — no
file, empty filename, disallowed extension. Each rejection must emit
EXACTLY one record at INFO or WARNING from the documents logger carrying
the rejection reason, the submitted filename (where present), the vault id,
and the requesting user id, while the HTTPException status/detail stay
byte-identical to the pre-fix contract (AC2).

AC2/AC3 structural guards: the only new log site is
``_log_upload_rejection`` — called exactly three times in
``documents.py``, all inside ``_do_upload`` (no per-success lines added),
and its arguments are metadata only (filename never file contents).

Enforcement boundary (swarm-pr-review PRR-001): AC3's "never file contents"
clause is enforced HERE, by ``_structural_guard`` walking **every**
argument of every call. ``test_no_query_text_in_default_logs.py`` is NOT a
substitute for that clause — its Guard A enumerates query-derived names
only and its own "Known limits" disclaims value-flow detection, so it stays
green while file bytes ride in a message body. That file is still untouched
by this PR (AC3's second half); it just does not carry the content clause.
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
from app.api.routes.documents import _do_upload, upload_document_root
from app.config import settings

VAULT_ID = 7
USER = {"id": 4242}
CONTENT_MARKER = "TOP-SECRET-FILE-CONTENTS-1041"

documents_logger_name = documents_module.logger.name

# AC2's "log no more than today" half, pinned as a count: ``_do_upload``
# carries exactly these four pre-existing ``logger.*`` sites at the anchor
# commit — the 409 duplicate warning (documents.py:2901) and the three
# error-path ``logger.exception`` calls (documents.py:2962, :2972, :2978).
# A fifth site means a new log line was added (a per-success line is the
# AC2 violation this pins). See test_do_upload_gains_no_new_log_site.
_PREEXISTING_LOGGER_SITES = 4

# AC1 allows INFO **or** WARNING; pinning to WARNING alone both rejects an
# AC1-legal INFO implementation and hides a duplicate INFO line (review
# PRR-003: both directions were executed as mutations M7 and M4).
_ACCEPTED_LEVELS = (logging.INFO, logging.WARNING)


def _rejection_records(caplog):
    return [r for r in caplog.records if r.name == documents_logger_name]


def _assert_single_rejection_line(caplog):
    records = _rejection_records(caplog)
    assert len(records) == 1, f"expected exactly one log line, got {records!r}"
    record = records[0]
    assert record.levelno in _ACCEPTED_LEVELS, (
        f"AC1 allows INFO or WARNING, got {record.levelno}"
    )
    return record.getMessage()


async def test_no_file_rejection_logs_one_line(caplog):
    request = SimpleNamespace(headers={})
    with caplog.at_level(logging.INFO, logger=documents_logger_name):
        with pytest.raises(HTTPException) as exc_info:
            await _do_upload(
                request, None, settings, None, None, None, None, VAULT_ID, USER
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "No file provided"

    message = _assert_single_rejection_line(caplog)
    assert "No file provided" in message
    assert f"vault_id={VAULT_ID}" in message
    assert "user_id=4242" in message


async def test_empty_filename_rejection_logs_one_line(caplog):
    request = SimpleNamespace(headers={})
    file = SimpleNamespace(filename="", file=io.BytesIO(CONTENT_MARKER.encode()))
    with caplog.at_level(logging.INFO, logger=documents_logger_name):
        with pytest.raises(HTTPException) as exc_info:
            await _do_upload(
                request, file, settings, None, None, None, None, VAULT_ID, USER
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Filename cannot be empty"

    message = _assert_single_rejection_line(caplog)
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
    # Content stream is present but must not be read into a log line.
    file = SimpleNamespace(
        filename="installer.exe", file=io.BytesIO(CONTENT_MARKER.encode())
    )
    try:
        with caplog.at_level(logging.INFO, logger=documents_logger_name):
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

    message = _assert_single_rejection_line(caplog)
    assert "File extension '.exe' not allowed" in message
    assert "installer.exe" in message
    assert f"vault_id={VAULT_ID}" in message
    assert "user_id=4242" in message
    # AC3: filenames/metadata only — never file contents.
    assert CONTENT_MARKER not in message


async def test_route_supplies_the_requesting_user(caplog, tmp_path):
    """AC1's user-id clause, exercised on the ROUTE seam (review PRR-004).

    The three checks above call ``_do_upload`` directly with a hand-built
    fixture dict, so they cannot tell a real requester id from a literal
    one. This one drives ``upload_document_root`` — the handler that owns
    the ``Depends(require_vault_permission("write"))`` principal — and
    asserts the id it passes is the id that reaches the log. Dropping the
    ``user`` argument from the route's ``_do_upload`` call therefore fails
    here with ``user_id=unknown``.
    """
    original_data_dir = settings.data_dir
    original_allowed = settings.allowed_extensions
    settings.data_dir = tmp_path
    settings.allowed_extensions = {".txt"}
    request = SimpleNamespace(headers={})
    file = SimpleNamespace(
        filename="installer.exe", file=io.BytesIO(CONTENT_MARKER.encode())
    )
    try:
        with caplog.at_level(logging.INFO, logger=documents_logger_name):
            with pytest.raises(HTTPException) as exc_info:
                await upload_document_root(
                    request,
                    file,
                    VAULT_ID,
                    settings,
                    None,
                    None,
                    None,
                    None,
                    USER,
                )
    finally:
        settings.data_dir = original_data_dir
        settings.allowed_extensions = original_allowed

    assert exc_info.value.status_code == 400

    message = _assert_single_rejection_line(caplog)
    assert "user_id=4242" in message, (
        "the route must thread its require_vault_permission principal into "
        "the rejection log, not fall back to 'unknown'"
    )


def _content_expression_problems(call: ast.Call) -> list[str]:
    """Content-bearing expressions in ANY argument of ``call``.

    Walks all four slots, not just the filename one, and matches on more
    than the literal ``.file`` attribute: ``file.file.read(8)`` and
    ``getattr(file, "file").read(8)`` both carry uploaded bytes into a
    message body while looking nothing like ``file.file`` at the surface
    (review PRR-001, executed as mutations M3 and M3b).
    """
    problems: list[str] = []
    for index, arg in enumerate(call.args):
        for node in ast.walk(arg):
            if isinstance(node, ast.Attribute) and node.attr in {"file", "read"}:
                problems.append(
                    f"args[{index}] reaches a content attribute "
                    f"{node.attr!r}: {ast.dump(node)}"
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in {"file", "read"}
            ):
                problems.append(
                    f"args[{index}] reaches content through a getattr alias "
                    f"{node.args[1].value!r}"
                )
    return problems


def _structural_guard(source: str) -> None:
    """AC2/AC3 structural assertions over ``documents.py`` source text.

    Kept separate from the test body so a planted-violation pin can feed it
    a synthetic tree and prove it actually fails (review PRR-001).
    """
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
        problems = _content_expression_problems(call)
        assert not problems, (
            "AC3: log arguments must be metadata only, never file contents — "
            + "; ".join(problems)
        )

    # AC2's "log no more than today" half (review PRR-002): the structural
    # guard above only counts ``_log_upload_rejection`` calls, so a plain
    # ``logger.warning(...)`` added anywhere in ``_do_upload`` — including
    # the success path — kept it green. Pin the total logger site count.
    logger_sites = [
        node
        for node in ast.walk(do_upload)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    ]
    assert len(logger_sites) == _PREEXISTING_LOGGER_SITES, (
        f"AC2: _do_upload must keep exactly {_PREEXISTING_LOGGER_SITES} "
        f"pre-existing logger sites, found {len(logger_sites)}: "
        f"{[(n.lineno, n.func.attr) for n in logger_sites]}"
    )


def test_rejection_log_helper_called_only_at_three_rejection_sites():
    """AC2/AC3 structural guard over the real ``documents.py``."""
    with open(documents_module.__file__, encoding="utf-8") as handle:
        _structural_guard(handle.read())


# Planted-violation pins: a guard that cannot fail for its named reason is
# not a guard. Each source below is a minimal ``_do_upload`` carrying one
# specific violation; the guard must reject it.

_VIOLATION_CONTENT_IN_REASON = '''
async def _do_upload(request, file, settings_dep, vector_store,
                     embedding_service, db_pool, background_processor,
                     vault_id, user=None):
    if file is None:
        _log_upload_rejection(
            f"peek={file.file.read(8)!r}", None, vault_id, user
        )
        raise HTTPException(status_code=400, detail="No file provided")
'''

_VIOLATION_CONTENT_VIA_GETATTR_IN_FILENAME_SLOT = '''
async def _do_upload(request, file, settings_dep, vector_store,
                     embedding_service, db_pool, background_processor,
                     vault_id, user=None):
    if not file.filename:
        _log_upload_rejection(
            "Filename cannot be empty", getattr(file, "file").read(8),
            vault_id, user
        )
        raise HTTPException(status_code=400, detail="Filename cannot be empty")
'''

_VIOLATION_EXTRA_LOGGER_SITE = '''
async def _do_upload(request, file, settings_dep, vector_store,
                     embedding_service, db_pool, background_processor,
                     vault_id, user=None):
    if file is None:
        _log_upload_rejection("No file provided", None, vault_id, user)
        raise HTTPException(status_code=400, detail="No file provided")
    logger.info("Upload accepted for %s", file.filename)
'''


@pytest.mark.parametrize(
    "violating_source",
    [
        _VIOLATION_CONTENT_IN_REASON,
        _VIOLATION_CONTENT_VIA_GETATTR_IN_FILENAME_SLOT,
        _VIOLATION_EXTRA_LOGGER_SITE,
    ],
    ids=["content-in-reason-slot", "content-via-getattr", "extra-logger-site"],
)
def test_structural_guard_rejects_a_planted_violation(violating_source):
    """The guard must FAIL on a tree that violates AC2/AC3.

    Without this pin the guard's claims are unfalsifiable: a mutation that
    slips past it (review PRR-001/PRR-002) would never be noticed by any
    gate in the repo.
    """
    with pytest.raises(AssertionError):
        _structural_guard(violating_source)
