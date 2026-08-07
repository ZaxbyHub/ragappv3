"""Draft Room parse-only isolation is unchanged when image ingestion is enabled
(issue #460).

Adding raster images to the vault upload allowlist must NOT change Draft Room's
own acceptance policy: the draft upload path filters raster images out of the
allowed list it passes to storage, so a standalone image is still rejected there
while a normal text document is accepted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.document_artifacts import RASTER_IMAGE_EXTENSIONS
from app.services.draft_input_storage import (
    DraftInputStorage,
    DraftInputUnsupportedError,
)


class _FakeUpload:
    def __init__(self, filename):
        self.filename = filename

    async def read(self, n):
        return b""


@pytest.fixture()
def storage(tmp_path):
    root = tmp_path / "draft-storage"
    root.mkdir()
    return DraftInputStorage(root)


def test_vault_allowlist_includes_images_but_draft_filter_removes_them():
    # The document feature enables raster images...
    assert RASTER_IMAGE_EXTENSIONS <= set(settings.allowed_extensions)
    # ...while Draft Room's own route allowlist excludes them. Importing the
    # real route constant (not re-deriving the filter locally) proves the
    # shipped policy stays in sync with the test (PRR-014).
    from app.api.routes.draft_room import DRAFT_ROOM_ALLOWED_EXTENSIONS

    assert not (set(RASTER_IMAGE_EXTENSIONS) & DRAFT_ROOM_ALLOWED_EXTENSIONS)


@pytest.mark.asyncio
async def test_draft_rooms_still_rejects_images(storage):
    # Uses the real route allowlist, so this asserts the shipped deployment
    # policy (not a locally re-computed one).
    from app.api.routes.draft_room import DRAFT_ROOM_ALLOWED_EXTENSIONS

    with pytest.raises(DraftInputUnsupportedError):
        await storage.stage_upload(
            _FakeUpload("photo.png"),
            allowed_extensions=DRAFT_ROOM_ALLOWED_EXTENSIONS,
            max_file_bytes=10 * 1024 * 1024,
        )


@pytest.mark.asyncio
async def test_draft_rooms_accepts_text_with_filtered_list(storage):
    from app.api.routes.draft_room import DRAFT_ROOM_ALLOWED_EXTENSIONS

    try:
        result = await storage.stage_upload(
            _FakeUpload("notes.txt"),
            allowed_extensions=DRAFT_ROOM_ALLOWED_EXTENSIONS,
            max_file_bytes=10 * 1024 * 1024,
        )
    except DraftInputUnsupportedError:
        # Text should not be blocked by the image filter; failure here means the
        # allowlist itself lost text types, which would be a regression.
        pytest.fail(".txt unexpectedly rejected by Draft Room filter")
    assert result is not None
