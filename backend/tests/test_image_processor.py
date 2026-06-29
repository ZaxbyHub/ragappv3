"""
Tests for image_processor module.

These tests verify:
- is_image_file() extension detection
- process_image() with real images (when Pillow is available)
- Graceful degradation when Pillow/pytesseract are not installed
- Corrupt / non-image file handling

All tests skip cleanly when optional dependencies are unavailable.
"""
from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

import pytest

# Ensure the app package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.image_processor import (
    SUPPORTED_IMAGE_TYPES,
    ImageProcessingResult,
    is_image_file,
    process_image,
)

# ---------------------------------------------------------------------------
# Optional dependency flags (mirrored from the module for test use)
# ---------------------------------------------------------------------------

_PIL_AVAILABLE = False
try:
    from PIL import Image as _PILImage

    _PIL_AVAILABLE = True
except ImportError:
    pass

_pytesseract_AVAILABLE = False
try:
    import pytesseract as _pytesseract

    _pytesseract_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_image_file(tmp_path: pytest.TempPathFactory) -> Path | None:
    """
    Create a minimal valid PNG image in a temporary directory.

    Returns the Path to the image, or None if Pillow is not available.
    """
    if not _PIL_AVAILABLE:
        return None
    from PIL import Image as PILImage

    img_path = tmp_path / "test.png"
    # 1x1 white pixel PNG
    img = PILImage.new("RGB", (1, 1), color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    img_path.write_bytes(buf.read())
    return img_path


@pytest.fixture
def tmp_corrupt_file(tmp_path: pytest.TempPathFactory) -> Path:
    """Return a path to a file that is not a valid image."""
    fake_path = tmp_path / "corrupt.png"
    fake_path.write_bytes(b"This is not an image file at all.")
    return fake_path


# ---------------------------------------------------------------------------
# is_image_file tests
# ---------------------------------------------------------------------------


class TestIsImageFile:
    """Tests for is_image_file()."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("photo.png", True),
            ("photo.PNG", True),
            ("photo.jpg", True),
            ("photo.JPG", True),
            ("photo.jpeg", True),
            ("photo.JPEG", True),
            ("photo.gif", True),
            ("photo.GIF", True),
            ("photo.bmp", True),
            ("photo.tiff", True),
            ("photo.webp", True),
            ("photo.webp", True),
            # Non-image extensions
            ("document.pdf", False),
            ("video.mp4", False),
            ("audio.mp3", False),
            ("text.txt", False),
            ("noextension", False),
            ("photo.png.extra", False),
        ],
    )
    def test_is_image_file(self, filename: str, expected: bool) -> None:
        """Correct extension detection for supported and unsupported types."""
        result = is_image_file(filename)
        assert result is expected

    def test_is_image_file_support_types_match_constant(self) -> None:
        """The SUPPORTED_IMAGE_TYPES constant and is_image_file are consistent."""
        for ext in SUPPORTED_IMAGE_TYPES:
            assert is_image_file(f"test{ext}") is True
            assert is_image_file(f"test{ext.upper()}") is True


# ---------------------------------------------------------------------------
# process_image tests — real image
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
class TestProcessImageReal:
    """Tests using a real in-memory image."""

    @pytest.mark.asyncio
    async def test_process_image_returns_success(self, tmp_image_file: Path | None) -> None:
        """Processing a valid small image returns success."""
        if tmp_image_file is None:
            pytest.skip("Pillow not available")

        result = await process_image(str(tmp_image_file))
        assert result.success is True
        assert result.error is None

    @pytest.mark.asyncio
    async def test_process_image_metadata_extracted(
        self, tmp_image_file: Path | None
    ) -> None:
        """Metadata (width, height, format, mode) is extracted."""
        if tmp_image_file is None:
            pytest.skip("Pillow not available")

        result = await process_image(str(tmp_image_file))
        assert result.success is True
        assert "width" in result.metadata
        assert "height" in result.metadata
        assert "format" in result.metadata
        assert "mode" in result.metadata
        assert result.metadata["width"] == 1
        assert result.metadata["height"] == 1

    @pytest.mark.asyncio
    async def test_process_image_nonexistent_file(self) -> None:
        """Non-existent file returns success=False with clear error."""
        result = await process_image("/does/not/exist.png")
        assert result.success is False
        assert "not found" in result.error
        assert result.extracted_text == ""
        assert result.metadata == {}


# ---------------------------------------------------------------------------
# process_image tests — graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    _PIL_AVAILABLE and _pytesseract_AVAILABLE,
    reason="Both Pillow and pytesseract are installed — degradation test not applicable",
)
class TestProcessImageGracefulDegradation:
    """Tests for graceful behaviour when optional libraries are missing."""

    @pytest.mark.asyncio
    async def test_no_pil_no_tesseract_error_message(self) -> None:
        """When neither library is installed, error message guides the user."""
        if _PIL_AVAILABLE or _pytesseract_AVAILABLE:
            pytest.skip("At least one library is available — degradation test not applicable")

        result = await process_image("any_file.png")
        assert result.success is False
        assert "Pillow" in result.error or "pytesseract" in result.error
        assert "not installed" in result.error


# ---------------------------------------------------------------------------
# process_image tests — corrupt / invalid input
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
class TestProcessImageCorrupt:
    """Tests for corrupt / non-image file handling."""

    @pytest.mark.asyncio
    async def test_corrupt_file_returns_failure(self, tmp_corrupt_file: Path) -> None:
        """A file that is not a valid image returns success=False with error detail."""
        result = await process_image(str(tmp_corrupt_file))
        assert result.success is False
        assert result.error is not None
        assert result.extracted_text == ""
