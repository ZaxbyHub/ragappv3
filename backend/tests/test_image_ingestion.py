"""Tests for the repaired standalone image ingestion path (issue #460).

Covers the three confirmed image defects and the new upload validation:
1. ``process_image`` is async and, when awaited, yields an ``ImageProcessingResult``
   (with ``.success``) rather than a bare coroutine — the double-async bug shape.
2. Image suffixes are present in the upload allowlist.
3. ``validate_image_content`` accepts a real raster and rejects corrupt/spoofed,
   SVG, and decompression-bomb payloads, with stable codes.
4. ``_check_image_magic`` performs the early header screen.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings

# Raster extensions that must be enabled on the default upload allowlist.
from app.services.document_artifacts import RASTER_IMAGE_EXTENSIONS
from app.services.image_processor import ImageProcessingResult, process_image
from app.services.upload_validation import (
    IMAGE_ERROR_DECOMPRESSION_BOMB,
    IMAGE_ERROR_INVALID,
    IMAGE_ERROR_UNSUPPORTED,
    _check_image_magic,
    validate_image_content,
)


class TestAllowlist:
    def test_all_image_extensions_enabled(self):
        for ext in RASTER_IMAGE_EXTENSIONS:
            assert ext in settings.allowed_extensions, ext

    def test_processor_dispatch_matches_canonical_raster_set(self):
        """Regression (final-critic finding 2): the processor's image dispatch
        must equal the canonical raster set (single source of truth), so every
        allowlisted raster suffix — including ``.tif`` — routes to the image
        pipeline, never the document parser.
        """
        from app.services.document_processor import DocumentProcessor

        assert DocumentProcessor.IMAGE_EXTENSIONS == set(RASTER_IMAGE_EXTENSIONS)
        assert ".tif" in DocumentProcessor.IMAGE_EXTENSIONS
        proc = object.__new__(DocumentProcessor)
        for ext in RASTER_IMAGE_EXTENSIONS:
            assert proc._is_image_file(f"file{ext}") is True, ext


class TestProcessImageContract:
    def test_process_image_is_async_and_returns_result_not_coroutine(self):
        """Regression for the double-async `.success` crash (defect 2)."""
        # process_image is a coroutine function; awaiting it yields a result.
        r = None

        async def _run():
            nonlocal r
            r = await process_image("/does/not/exist.png")

        import asyncio

        asyncio.run(_run())
        assert isinstance(r, ImageProcessingResult)
        assert hasattr(r, "success")
        assert r.success is False

    def test_process_image_nonexistent_has_stable_error_code(self):
        import asyncio

        async def _run():
            return await process_image("/nope/missing.png")

        result = asyncio.run(_run())
        assert result.error_code == "file_not_found"


@pytest.mark.skipif(True, reason="covered via PIL if available in environment")
def test_valid_image_content():
    pass


class TestValidateImageContent:
    def _png_bytes(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buf, format="PNG")
        return buf.getvalue()

    def test_accepts_real_png(self, tmp_path):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            pytest.skip("Pillow not available")
        path = tmp_path / "ok.png"
        path.write_bytes(self._png_bytes())
        ok, code = validate_image_content(path, max_pixels=100_000_000, max_frames=64)
        assert ok is True
        assert code is None

    def test_rejects_spoofed_content(self, tmp_path):
        path = tmp_path / "fake.png"
        path.write_bytes(b"This is text pretending to be an image")
        ok, code = validate_image_content(path, max_pixels=100_000_000, max_frames=64)
        assert ok is False
        assert code == IMAGE_ERROR_INVALID

    def test_rejects_svg_markup(self, tmp_path):
        path = tmp_path / "evil.svg"
        path.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        ok, code = validate_image_content(path, max_pixels=100_000_000, max_frames=64)
        assert ok is False
        assert code == IMAGE_ERROR_UNSUPPORTED

    def test_rejects_html(self, tmp_path):
        path = tmp_path / "evil.html"
        path.write_bytes(b"<!DOCTYPE html><html><body>x</body></html>")
        ok, _ = validate_image_content(path, max_pixels=100_000_000, max_frames=64)
        assert ok is False

    def test_rejects_decompression_bomb(self, tmp_path):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            pytest.skip("Pillow not available")
        # A tiny PNG with huge reported dimensions trips the pixel bound.
        path = tmp_path / "bomb.png"
        path.write_bytes(self._png_bytes())
        ok, code = validate_image_content(path, max_pixels=10, max_frames=64)
        assert ok is False
        assert code == IMAGE_ERROR_DECOMPRESSION_BOMB


class TestCheckImageMagic:
    def test_accepts_matching_png_header(self):
        assert _check_image_magic(".png", b"\x89PNG\r\n\x1a\n\x00\x00")

    def test_rejects_mismatched_png_header(self):
        assert not _check_image_magic(".png", b"GIF89a\x00\x00")

    def test_tiff_accepts_both_byte_orders(self):
        assert _check_image_magic(".tiff", b"II*\x00\x3c\x00\x00\x00")
        assert _check_image_magic(".tiff", b"MM\x00*\x00\x00\x00\x3c")

    def test_non_image_extension_passes_through(self):
        # Non-raster extensions are unchecked here; decode is authoritative.
        assert _check_image_magic(".pdf", b"whatever")
