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


def _filtered_allowed() -> set:
    # Mirrors the Draft Room upload route: image suffixes removed from the
    # global (now image-inclusive) allowlist.
    return set(settings.allowed_extensions) - set(RASTER_IMAGE_EXTENSIONS)


@pytest.fixture()
def storage(tmp_path):
    root = tmp_path / "draft-storage"
    root.mkdir()
    return DraftInputStorage(root)


def test_vault_allowlist_includes_images_but_draft_filter_removes_them():
    # The document feature enables raster images...
    assert RASTER_IMAGE_EXTENSIONS <= set(settings.allowed_extensions)
    # ...while Draft Room's filtered list excludes them.
    assert not (set(RASTER_IMAGE_EXTENSIONS) & _filtered_allowed())


@pytest.mark.asyncio
async def test_draft_rooms_still_rejects_images(storage):
    with pytest.raises(DraftInputUnsupportedError):
        await storage.stage_upload(
            _FakeUpload("photo.png"),
            allowed_extensions=_filtered_allowed(),
            max_file_bytes=10 * 1024 * 1024,
        )


@pytest.mark.asyncio
async def test_draft_rooms_accepts_text_with_filtered_list(storage):
    try:
        result = await storage.stage_upload(
            _FakeUpload("notes.txt"),
            allowed_extensions=_filtered_allowed(),
            max_file_bytes=10 * 1024 * 1024,
        )
    except DraftInputUnsupportedError:
        # Text should not be blocked by the image filter; failure here means the
        # allowlist itself lost text types, which would be a regression.
        pytest.fail(".txt unexpectedly rejected by Draft Room filter")
    assert result is not None
