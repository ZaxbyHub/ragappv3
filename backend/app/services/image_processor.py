"""
Standalone image processing module for FR-009 (image ingestion).

Detects image files and extracts searchable text via OCR (pytesseract) and/or
image metadata using Pillow. Both Pillow and pytesseract are optional dependencies;
the module imports gracefully even if they are not installed.

Usage:
    from app.services.image_processor import is_image_file, process_image, ImageProcessingResult

    if is_image_file("photo.png"):
        result = await process_image("photo.png")
        if result.success:
            print(result.extracted_text)
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from app.services.document_artifacts import RASTER_IMAGE_EXTENSIONS
from app.services.upload_validation import validate_image_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency availability
# ---------------------------------------------------------------------------

_PIL_AVAILABLE = False
_pytesseract_AVAILABLE = False

try:
    from PIL import Image as _PILImage

    _PIL_AVAILABLE = True
except ImportError:
    _PILImage = None  # type: ignore[assignment]

try:
    import pytesseract as _pytesseract

    _pytesseract_AVAILABLE = True
except ImportError:
    _pytesseract = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Canonical raster image extension registry shared with the upload allowlist,
# magic checks, and content validation (issue #460). Includes both .tif/.tiff.
SUPPORTED_IMAGE_TYPES: set[str] = set(RASTER_IMAGE_EXTENSIONS)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ImageProcessingResult:
    """
    Result of processing an image file.

    Attributes:
        extracted_text: Text extracted via OCR. Empty string if no text found
            or if processing failed.
        metadata: Dictionary with image metadata (width, height, format, mode).
            Empty dict if metadata could not be extracted.
        success: True when the image was processed successfully (even if no
            text was found), False when processing failed.
        error: Bounded, human-readable error message. None when success is True.
        error_code: Stable machine-readable failure code (one of the
            ``ERROR_*`` module constants), or None on success.
    """

    extracted_text: str
    metadata: dict
    success: bool
    error: Optional[str]
    error_code: Optional[str] = None


# Stable, bounded, machine-readable failure codes (issue #460). Error messages
# are capped at MAX_ERROR_MESSAGE_CHARS so a hostile image can never inflate
# logged/persisted payloads.
ERROR_MISSING_LIBRARY = "missing_library"
ERROR_FILE_NOT_FOUND = "file_not_found"
ERROR_INVALID_IMAGE = "invalid_image"
ERROR_OCR_FAILED = "ocr_failed"
ERROR_UNSUPPORTED = "unsupported_image"
MAX_ERROR_MESSAGE_CHARS = 200


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_image_file(file_path: str) -> bool:
    """
    Check whether a file path has a supported image extension.

    Args:
        file_path: Path to the file (may be absolute or relative).

    Returns:
        True if the file extension (lowercased) is in SUPPORTED_IMAGE_TYPES,
        False otherwise.
    """
    _, ext = os.path.splitext(file_path)
    return ext.lower() in SUPPORTED_IMAGE_TYPES


def _bounded_error(message: str) -> str:
    """Cap an error message so it is bounded and content-free of image payload."""
    return message[:MAX_ERROR_MESSAGE_CHARS]


async def process_image(file_path: str) -> ImageProcessingResult:
    """
    Process a single image file and extract searchable text.

    This is the async public contract (issue #460). The actual PIL/pytesseract
    work is blocking, so it is delegated to the synchronous
    :func:`_process_image_sync` helper via :func:`asyncio.to_thread` — keeping
    the event loop responsive while OCR runs.

    Extraction order:
      1. OCR via pytesseract (if available).
      2. If pytesseract is not available, falls back to returning empty text.

    Metadata (width, height, format, mode) is always extracted from PIL when
    Pillow is available.

    Args:
        file_path: Absolute or relative path to the image file.

    Returns:
        An ImageProcessingResult instance describing the outcome, with stable
        ``error_code`` on failure.
    """
    return await asyncio.to_thread(_process_image_sync, file_path)


def _process_image_sync(file_path: str) -> ImageProcessingResult:
    """
    Synchronous implementation of image processing (blocking PIL/pytesseract).

    Runs off the event loop inside :func:`process_image`. Kept private; callers
    must use the async :func:`process_image` so the pipeline never mis-uses
    ``asyncio.to_thread`` on an already-async function (issue #460 defect 2).
    """
    if not _PIL_AVAILABLE and not _pytesseract_AVAILABLE:
        return ImageProcessingResult(
            extracted_text="",
            metadata={},
            success=False,
            error="Image processing libraries not installed. Install Pillow + pytesseract.",
            error_code=ERROR_MISSING_LIBRARY,
        )

    if not os.path.exists(file_path):
        return ImageProcessingResult(
            extracted_text="",
            metadata={},
            success=False,
            error=f"File not found: {file_path}",
            error_code=ERROR_FILE_NOT_FOUND,
        )

    extracted_text = ""
    metadata: dict = {}

    # Open image and extract metadata
    if _PIL_AVAILABLE:
        # Authoritative raster gate (decompression-bomb / frame-count / real
        # decode) BEFORE the expensive OCR. Mirrors the upload-route check so
        # ingestion paths that bypass the upload gate (FileWatcher, email) cannot
        # skip it and force an unbounded decode (issue #460 review, PRR-003).
        valid, vc_error = validate_image_content(file_path)
        if not valid:
            return ImageProcessingResult(
                extracted_text="",
                metadata={},
                success=False,
                error=_bounded_error(str(vc_error) or "Image rejected"),
                error_code=ERROR_INVALID_IMAGE,
            )
        try:
            with _PILImage.open(file_path) as img:
                metadata = {
                    "width": img.width,
                    "height": img.height,
                    "format": img.format or "unknown",
                    "mode": img.mode,
                }

                if _pytesseract_AVAILABLE:
                    try:
                        extracted_text = _pytesseract.image_to_string(img)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("OCR failed for %s: %s", file_path, exc)
                        # Non-fatal: we still return the metadata
        except Exception as exc:  # noqa: BLE001
            return ImageProcessingResult(
                extracted_text="",
                metadata={},
                success=False,
                error=_bounded_error(f"Failed to open image: {exc}"),
                error_code=ERROR_INVALID_IMAGE,
            )
    else:
        # PIL not available but pytesseract is — try to run OCR directly.
        # pytesseract can accept a file path directly, but without PIL we
        # cannot extract metadata.
        if _pytesseract_AVAILABLE:
            try:
                extracted_text = _pytesseract.image_to_string(file_path)
            except Exception as exc:  # noqa: BLE001
                return ImageProcessingResult(
                    extracted_text="",
                    metadata={},
                    success=False,
                    error=_bounded_error(f"OCR failed: {exc}"),
                    error_code=ERROR_OCR_FAILED,
                )

    return ImageProcessingResult(
        extracted_text=extracted_text,
        metadata=metadata,
        success=True,
        error=None,
    )
