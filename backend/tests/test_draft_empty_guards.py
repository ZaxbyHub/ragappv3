
"""Item 0b tests: hard failures for the empty-candidate chain."""
import pytest

from app.api.routes import draft_room as dr
from app.services.draft_pipeline import CompileFailure, _assert_nonempty_candidate


def test_empty_candidate_guard_raises():
    with pytest.raises(CompileFailure) as exc:
        _assert_nonempty_candidate("", "copy")
    assert exc.value.code == "candidate_empty"
    assert not exc.value.retryable


def test_whitespace_candidate_guard_raises():
    with pytest.raises(CompileFailure):
        _assert_nonempty_candidate("   \n  ", "assemble")


def test_nonempty_candidate_passes():
    _assert_nonempty_candidate("real text", "fact")


def test_empty_revision_guard_raises():
    class Row(dict):
        def __init__(self):
            super().__init__(
                content_md="",
                content_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
    with pytest.raises(dr.DraftRoomHTTPError) as exc:
        dr._assert_revision_nonempty(Row())
    assert exc.value.status_code == 409
    assert getattr(exc.value, "code", None) == "revision_empty"


def test_rewrite_without_manuscript_rejected():
    class Cursor:
        def fetchone(self):
            return (0,)

    class Cur:
        def execute(self, q, params):
            return Cursor()

    with pytest.raises(dr.DraftRoomHTTPError) as exc:
        dr._assert_rewrite_has_manuscript(1, Cur())  # type: ignore[arg-type]
    assert exc.value.status_code == 422
    assert getattr(exc.value, "code", None) == "rewrite_requires_manuscript"
