
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


# ── the four stage methods themselves refuse an empty candidate ────────────
# These drive the real methods on a bare instance (no stubbed guard): the
# assertion must be a live first statement of each stage, with the correct
# stage name in the message. Built with __new__ precisely because the guard
# must fire before anything else the stage touches.


def _call_stage(method, kwargs):
    import asyncio

    from app.services import draft_pipeline as dp

    obj = dp._CompileRun.__new__(dp._CompileRun)
    object.__setattr__(obj, "_candidate", "")
    coro = getattr(obj, method)(**kwargs)
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "method,kwargs,stage",
    [
        ("_run_copy", {"reason": "test"}, "copy"),
        ("_run_standards", {"reason": "test"}, "standards"),
        ("_stage_fact", {}, "fact"),
        ("_stage_assemble", {}, "assemble"),
    ],
)
def test_stage_methods_refuse_empty_candidate(method, kwargs, stage):
    with pytest.raises(CompileFailure) as exc:
        _call_stage(method, kwargs)
    assert exc.value.code == "candidate_empty"
    assert not exc.value.retryable
    assert f"the {stage} stage" in str(exc.value)
