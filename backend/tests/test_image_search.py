"""
Tests for image_search module.

Verifies:
- build_searchable_text() text construction
- embed_image_content() embedding pipeline (mocked embedding service)
- prepare_image_for_indexing() full pipeline (mocked process_image + embedding service)
- Error tolerance when process_image fails

No real PIL/pytesseract needed — process_image is mocked throughout.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the app package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.image_processor import ImageProcessingResult
from app.services.image_search import (
    ImageIndexEntry,
    build_searchable_text,
    embed_image_content,
    prepare_image_for_indexing,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedding_service():
    """Return a mock EmbeddingService that returns a fixed vector."""
    svc = MagicMock()
    # embed_single is async, so mock it as an async function
    fake_vector = [0.1] * 10
    svc.embed_single = AsyncMock(return_value=fake_vector)
    return svc


# ---------------------------------------------------------------------------
# Tests: build_searchable_text
# ---------------------------------------------------------------------------


class TestBuildSearchableText:
    def test_with_ocr_text_contains_ocr_text(self):
        """When OCR text is present it appears in the output."""
        result = ImageProcessingResult(
            extracted_text="Receipt for groceries",
            metadata={"width": 800, "height": 600, "format": "PNG", "mode": "RGB"},
            success=True,
            error=None,
        )
        text = build_searchable_text(result, "receipt.png")
        assert "Receipt for groceries" in text
        assert "receipt.png" in text
        assert "800x600" in text
        assert "PNG format" in text

    def test_without_ocr_contains_metadata(self):
        """When OCR text is empty, metadata is still represented."""
        result = ImageProcessingResult(
            extracted_text="",
            metadata={"width": 1920, "height": 1080, "format": "JPEG", "mode": "RGB"},
            success=True,
            error=None,
        )
        text = build_searchable_text(result, "photo.jpg")
        assert "photo.jpg" in text
        assert "1920x1080" in text
        assert "JPEG format" in text
        assert "No text extracted." in text

    def test_empty_result_produces_minimal_text(self):
        """Empty result still yields a minimal searchable representation."""
        result = ImageProcessingResult(
            extracted_text="",
            metadata={},
            success=True,
            error=None,
        )
        text = build_searchable_text(result, "empty.png")
        assert "empty.png" in text
        assert "No text extracted." in text

    def test_partial_metadata_includes_available_fields(self):
        """When only some metadata fields are present, include what is available."""
        result = ImageProcessingResult(
            extracted_text="",
            metadata={"width": 100, "format": "GIF"},
            success=True,
            error=None,
        )
        text = build_searchable_text(result, "anim.gif")
        assert "anim.gif" in text
        assert "100" in text
        assert "GIF format" in text


# ---------------------------------------------------------------------------
# Tests: embed_image_content
# ---------------------------------------------------------------------------


class TestEmbedImageContent:
    @pytest.mark.asyncio
    async def test_calls_embed_single_with_searchable_text(self, mock_embedding_service):
        """embed_single is called with the result of build_searchable_text."""
        result = ImageProcessingResult(
            extracted_text="Invoice #1234",
            metadata={"width": 210, "height": 297, "format": "PNG", "mode": "RGBA"},
            success=True,
            error=None,
        )
        vector = await embed_image_content(result, "invoice.png", mock_embedding_service)

        # Verify embed_single was called once
        mock_embedding_service.embed_single.assert_called_once()

        # Verify the text fed to embed_single contains the OCR and filename
        call_args = mock_embedding_service.embed_single.call_args
        embedded_text = call_args[0][0]  # first positional arg
        assert "invoice.png" in embedded_text
        assert "Invoice #1234" in embedded_text

        # Verify the returned vector is the fake vector
        assert vector == [0.1] * 10

    @pytest.mark.asyncio
    async def test_returns_vector_of_correct_dimension(self, mock_embedding_service):
        """The returned vector has the dimension of the mock vector."""
        result = ImageProcessingResult(
            extracted_text="",
            metadata={},
            success=True,
            error=None,
        )
        vector = await embed_image_content(result, "blank.png", mock_embedding_service)
        assert len(vector) == 10


# ---------------------------------------------------------------------------
# Tests: prepare_image_for_indexing
# ---------------------------------------------------------------------------


class TestPrepareImageForIndexing:
    @pytest.mark.asyncio
    async def test_returns_image_index_entry_with_correct_fields(self, mock_embedding_service):
        """Returns an ImageIndexEntry with all expected fields populated."""
        with patch(
            "app.services.image_search.process_image",
            new_callable=AsyncMock,
        ) as mock_process:
            mock_process.return_value = ImageProcessingResult(
                extracted_text="Meeting notes",
                metadata={"width": 1280, "height": 720, "format": "PNG", "mode": "RGB"},
                success=True,
                error=None,
            )

            entry = await prepare_image_for_indexing(
                "/uploads/notes.png", "notes.png", mock_embedding_service
            )

        assert isinstance(entry, ImageIndexEntry)
        assert entry.source_file_path == "/uploads/notes.png"
        assert entry.success is True
        assert entry.error is None
        assert entry.metadata == {"width": 1280, "height": 720, "format": "PNG", "mode": "RGB"}
        assert "Meeting notes" in entry.text_representation
        assert entry.embedding == [0.1] * 10

    @pytest.mark.asyncio
    async def test_error_tolerance_process_image_fails(self, mock_embedding_service):
        """When process_image fails, success=False and error is populated."""
        with patch(
            "app.services.image_search.process_image",
            new_callable=AsyncMock,
        ) as mock_process:
            mock_process.return_value = ImageProcessingResult(
                extracted_text="",
                metadata={},
                success=False,
                error="File not found: /missing/image.png",
            )

            entry = await prepare_image_for_indexing(
                "/missing/image.png", "image.png", mock_embedding_service
            )

        assert isinstance(entry, ImageIndexEntry)
        assert entry.success is False
        assert "File not found" in entry.error
        assert entry.text_representation == ""
        assert entry.embedding == []
        assert entry.metadata == {}

    @pytest.mark.asyncio
    async def test_error_tolerance_embedding_fails(self, mock_embedding_service):
        """When embedding fails, success=False with error message and partial entry preserved."""
        with patch(
            "app.services.image_search.process_image",
            new_callable=AsyncMock,
        ) as mock_process:
            mock_process.return_value = ImageProcessingResult(
                extracted_text="Partial text",
                metadata={"width": 100, "height": 100, "format": "PNG", "mode": "RGB"},
                success=True,
                error=None,
            )

            # Make embed_single raise an exception
            mock_embedding_service.embed_single = AsyncMock(
                side_effect=Exception("Embedding service unavailable")
            )

            entry = await prepare_image_for_indexing(
                "/uploads/broken.png", "broken.png", mock_embedding_service
            )

        assert entry.success is False
        assert "Embedding failed" in entry.error
        # Text representation is still built even if embedding fails
        assert "broken.png" in entry.text_representation
        assert entry.embedding == []
        assert entry.metadata == {"width": 100, "height": 100, "format": "PNG", "mode": "RGB"}
