"""Regression tests for the Draft Room error-map helpers' 500 detail (issue #917).

``_map_store_error`` and ``_map_storage_error`` translate store/storage
failures into :class:`DraftRoomHTTPError`. Their unmapped-code fallback arms
return status 500 — and before issue #917 they put ``str(exc)`` in the
client-visible detail, echoing raw store internals (driver messages, SQL
fragments) to API clients. The fix returns a generic detail on the >= 500 arm
while the 4xx arms keep their crafted ``str(exc)`` messages; the server-side
cause log already exists (every helper result is raised ``from exc`` and the
``draft_room_exception_handler`` >= 500 branch logs ``exc.__cause__``).

These tests pin: 500 + stable ``code``/``context`` + a generic detail that
does not carry the exception text, and the unchanged 4xx details.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.draft_room import (  # noqa: E402
    _map_storage_error,
    _map_store_error,
)
from app.services.draft_input_storage import (  # noqa: E402
    DraftInputPathError,
    DraftInputStorageError,
    DraftInputTooLargeError,
    DraftInputUnsupportedError,
)
from app.services.draft_store import (  # noqa: E402
    DraftConflictError,
    DraftLimitExceededError,
    DraftNotFoundError,
    DraftStoreError,
    DraftValidationError,
)

MARKER = "MARKER-RAW-9331"


def test_unmapped_store_error_maps_to_500_with_a_generic_detail() -> None:
    exc = DraftStoreError(f"sqlite driver blew up {MARKER}")
    mapped = _map_store_error(exc)
    assert mapped.status_code == 500
    assert mapped.code == "internal_error"
    assert MARKER not in (mapped.detail or ""), (
        "the >= 500 arm must not echo raw exception text to the client"
    )
    assert mapped.detail == "draft store operation failed"


def test_store_error_keeps_code_and_context_fields() -> None:
    exc = DraftStoreError(f"boom {MARKER}")
    setattr(exc, "existing_input_id", 42)
    mapped = _map_store_error(exc)
    assert mapped.status_code == 500
    assert mapped.code == "internal_error"
    assert mapped.context == {"existing_input_id": 42}


def test_mapped_store_errors_keep_their_crafted_details() -> None:
    """4xx arms keep ``str(exc)`` — those messages are crafted store messages,
    not server-fault echoes."""
    cases = [
        (DraftNotFoundError(f"draft not found {MARKER}"), 404, "not_found"),
        (DraftValidationError(f"invalid {MARKER}"), 422, "validation_failed"),
        (DraftConflictError(f"conflict {MARKER}"), 409, "conflict"),
        (DraftLimitExceededError(f"limit exceeded {MARKER}"), 413, "limit_exceeded"),
    ]
    for exc, expected_status, expected_code in cases:
        mapped = _map_store_error(exc)
        assert mapped.status_code == expected_status, type(exc).__name__
        assert mapped.code == expected_code, type(exc).__name__
        assert MARKER in mapped.detail, (
            f"{type(exc).__name__}: 4xx arm should keep the crafted str(exc) detail"
        )


def test_unmapped_storage_error_maps_to_500_with_a_generic_detail() -> None:
    exc = DraftInputStorageError(f"disk read failed {MARKER}")
    mapped = _map_storage_error(exc)
    assert mapped.status_code == 500
    assert mapped.code == "internal_error"
    assert MARKER not in (mapped.detail or "")
    assert mapped.detail == "draft input storage failed"


def test_mapped_storage_errors_keep_their_crafted_details() -> None:
    cases = [
        (DraftInputTooLargeError(f"too large {MARKER}"), 413, "input_too_large"),
        (DraftInputUnsupportedError(f"bad type {MARKER}"), 415, "unsupported_input"),
        (DraftInputPathError(f"bad path {MARKER}"), 400, "invalid_storage_path"),
    ]
    for exc, expected_status, expected_code in cases:
        mapped = _map_storage_error(exc)
        assert mapped.status_code == expected_status, type(exc).__name__
        assert mapped.code == expected_code, type(exc).__name__
        assert MARKER in mapped.detail
