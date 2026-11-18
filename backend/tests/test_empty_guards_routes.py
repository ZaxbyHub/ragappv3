"""Route-level empty-revision guards (."""
import pytest

from app.api.routes.draft_room import DraftRoomHTTPError, _assert_revision_nonempty
from app.services.draft_pipeline import CompileFailure, _assert_nonempty_candidate
from app.services.draft_store import DraftRevisionRecord


class _StubStore:
    """Records calls. Serves a revision carrying the given text."""

    def __init__(self, text):
        self.calls = []
        self.text = text

    def _record(self, rid):
        return DraftRevisionRecord(
            id=rid,
            draft_id=1,
            parent_revision_id=None,
            job_id=1,
            revision_no=1,
            source="compile",
            content_sha256="a" * 64,
            fact_status="passed",
            is_current=1,
            created_by=1,
            created_at="2026-11-15T00:00:00Z",
            content_md=self.text,
        )

    async def get_revision(self, draft_id, owner_id, revision_id):
        self.calls.append("get_revision")
        return self._record(revision_id)

    async def get_current_revision(self, draft_id, owner_id):
        self.calls.append("get_current_revision")
        return self._record(1)

    def mark_ready(self, *a, **k):
        self.calls.append("mark_ready")

    def promote(self, *a, **k):
        self.calls.append("promote")


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_ready_route_sequence_rejects_empty_revision():
    """The Mark Ready sequence: fetch the REQUESTED revision, guard, then mutate.
    Empty text -> revision_empty and mark_ready is never called."""
    store = _StubStore("   ")
    rev = _run(store.get_revision(20, 1, 5))
    with pytest.raises(DraftRoomHTTPError) as ei:
        _assert_revision_nonempty(rev)
    assert ei.value.code == "revision_empty"
    assert "mark_ready" not in store.calls


def test_export_route_sequence_rejects_empty_revision():
    store = _StubStore("")
    rev = _run(store.get_revision(20, 1, 5))
    with pytest.raises(DraftRoomHTTPError) as ei:
        _assert_revision_nonempty(rev)
    assert ei.value.code == "revision_empty"


def test_promote_route_sequence_rejects_empty_current_revision():
    store = _StubStore("\n\n")
    rev = _run(store.get_current_revision(20, 1))
    with pytest.raises(DraftRoomHTTPError) as ei:
        _assert_revision_nonempty(rev)
    assert ei.value.code == "revision_empty"
    assert "promote" not in store.calls


def test_nonempty_revision_passes_all_three_routes():
    store = _StubStore("The annex custodian gap is documented in the source.")
    for rev in (
        _run(store.get_revision(21, 1, 4)),
        _run(store.get_current_revision(21, 1)),
    ):
        _assert_revision_nonempty(rev)  # no raise


@pytest.mark.parametrize("stage", ["copy", "standards", "fact", "assemble"])
def test_candidate_empty_at_call_site(stage):
    """Each of the four call sites passes its stage name; empty fails it."""
    with pytest.raises(CompileFailure) as ei:
        _assert_nonempty_candidate("", stage)
    assert ei.value.code == "candidate_empty"


def test_whitespace_candidate_rejected_at_copy():
    with pytest.raises(CompileFailure) as ei:
        _assert_nonempty_candidate("  \n\t ", "copy")
    assert ei.value.code == "candidate_empty"


def test_real_candidate_passes_all_four_sites():
    for stage in ("copy", "standards", "fact", "assemble"):
        _assert_nonempty_candidate("Supported sentence.", stage)


def test_draft_empty_zero_sections_fails_stage():
    from app.services.draft_pipeline import _guard_outline_mode_and_sections

    class _A:
        mode = "compose"
        sections = []

    class _C:
        mode = "compose"
        manuscript_inputs = [object()]

    with pytest.raises(CompileFailure) as ei:
        _guard_outline_mode_and_sections(_A(), _C())
    assert ei.value.code == "outline_empty"
