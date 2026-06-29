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

import logging
import os
from dataclasses import dataclass
from typing import Optional

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

SUPPORTED_IMAGE_TYPES: set[str] = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}

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
        error: Human-readable error message. None when success is True.
    """

    extracted_text: str
    metadata: dict
    success: bool
    error: Optional[str]


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


async def process_image(file_path: str) -> ImageProcessingResult:
    """
    Process a single image file and extract searchable text.

    Extraction order:
      1. OCR via pytesseract (if available).
      2. If pytesseract is not available, falls back to returning empty text.

    Metadata (width, height, format, mode) is always extracted from PIL when
    Pillow is available.

    Args:
        file_path: Absolute or relative path to the image file.

    Returns:
        An ImageProcessingResult instance describing the outcome.
    """
    if not _PIL_AVAILABLE and not _pytesseract_AVAILABLE:
        return ImageProcessingResult(
            extracted_text="",
            metadata={},
            success=False,
            error="Image processing libraries not installed. Install Pillow + pytesseract.",
        )

    if not os.path.exists(file_path):
        return ImageProcessingResult(
            extracted_text="",
            metadata={},
            success=False,
            error=f"File not found: {file_path}",
        )

    extracted_text = ""
    metadata: dict = {}

    # Open image and extract metadata
    if _PIL_AVAILABLE:
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
                error=f"Failed to open image: {exc}",
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
                    error=f"OCR failed: {exc}",
                )

    return ImageProcessingResult(
        extracted_text=extracted_text,
        metadata=metadata,
        success=True,
        error=None,
    )
