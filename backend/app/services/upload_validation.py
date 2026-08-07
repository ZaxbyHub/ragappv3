"""
Shared upload validation helpers.

Extension/content consistency checks and filename sanitization used by the
document upload path. Kept in a service module so non-route callers can reuse
the exact same validation logic.
"""

import os
import re

# Magic byte signatures for file types where extension spoofing is high-risk.
# Text-based formats (txt, md, csv, json, yaml, etc.) have no fixed binary header
# and are intentionally excluded from this check.
_MAGIC_BYTES: dict[str, bytes] = {
    ".pdf": b"%PDF",
    ".docx": b"PK\x03\x04",
    ".xlsx": b"PK\x03\x04",
    ".pptx": b"PK\x03\x04",
    ".xls": b"\xd0\xcf\x11\xe0",  # OLE Compound File
}

# OOXML formats are ZIP containers; the generic PK\x03\x04 signature only
# proves the upload is *a* ZIP, not a well-formed docx/xlsx/pptx. Map each
# extension to the format-specific member that must be present (B3-1).
_OOXML_REQUIRED_MEMBERS: dict[str, str] = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}

# Raster image extensions accepted through the standalone image ingestion path
# (issue #460); the canonical registry lives in app/services/document_artifacts.
# Per-extension accepted header prefixes for an *early* extension/header
# consistency screen at upload time. The authoritative check is the real image
# decode in :func:`validate_image_content`; these just reject obvious polyglots.
# TIFF accepts both byte orders (II*\x00 little-endian, MM\x00* big-endian).
_IMAGE_MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".webp": (b"RIFF",),
}

# Stable, bounded image validation failure codes (issue #460).
IMAGE_ERROR_INVALID = "invalid_image"
IMAGE_ERROR_UNSUPPORTED = "unsupported_image"
IMAGE_ERROR_DECOMPRESSION_BOMB = "decompression_bomb"
IMAGE_ERROR_TOO_MANY_FRAMES = "too_many_frames"
IMAGE_ERROR_MISSING_LIBRARY = "missing_library"

# Header prefixes of formats that must never be accepted in the raster artifact
# path (SVG/HTML are vector/markup, not raster; decode rejects them, this is a
# fast guard).
_RASTER_REJECT_HEADERS: tuple[bytes, ...] = (
    b"<svg",
    b"<?xml",
    b"<!DOCTYPE html",
    b"<html",
    b"<!doctype html",
)


def _check_magic_bytes(extension: str, header: bytes) -> bool:
    """Return True if header matches expected magic bytes for the extension.

    For OOXML formats (.docx/.xlsx/.pptx) this only verifies the generic ZIP
    signature; full structural validation (required member presence) is done
    post-write by :func:`_validate_ooxml_member` (B3-1), because the ZIP
    central directory lives at the end of the file and cannot be inspected
    from the 8-byte upload-time header.
    """
    magic = _MAGIC_BYTES.get(extension)
    if magic is None:
        return True
    return header[: len(magic)] == magic


def _validate_ooxml_member(path, required_member: str) -> bool:
    """Return True if the ZIP at ``path`` contains ``required_member`` (B3-1).

    Fail-closed: any zip/IO error returns False so a malformed archive is
    rejected rather than reaching the parser. An entry-count guard caps the
    cost of inspecting a maliciously large central directory.
    """
    import zipfile

    try:
        with zipfile.ZipFile(str(path)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    if len(names) > 10000:
        return False
    return any(name == required_member for name in names)


def _check_image_magic(extension: str, header: bytes) -> bool:
    """True if ``header`` matches an accepted image prefix for ``extension``.

    For non-raster extensions (or raster extensions with no configured
    signature) this returns True so the caller's authoritative content check is
    the decoder, not this cheap screen. Used only as an early polyglot guard.
    """
    signatures = _IMAGE_MAGIC_BYTES.get(extension)
    if not signatures:
        return True
    return any(header.startswith(sig) for sig in signatures)


def _detect_image_reject_header(header: bytes) -> bool:
    """True if ``header`` looks like vector/markup that is not a raster image."""
    lowered = header.lower()
    return any(lowered.startswith(sig) for sig in _RASTER_REJECT_HEADERS)


def validate_image_content(
    path,
    *,
    max_pixels: int | None = None,
    max_frames: int | None = None,
) -> tuple[bool, str | None]:
    """Authoritatively verify ``path`` decodes as a bounded raster image.

    Suffix acceptance alone is insufficient (issue #460): a spoofed/corrupt file
    that merely carries a ``.png``/``.jpg`` name must be rejected. This actually
    decodes the image (PIL) under decompression-bomb and frame-count bounds and
    rejects SVG/HTML and any non-raster payload.

    Returns ``(True, None)`` for a valid raster image, or ``(False, error_code)``
    with a stable :data:`IMAGE_ERROR_*` code. When Pillow is unavailable it
    returns ``IMAGE_ERROR_MISSING_LIBRARY`` so an image upload is rejected rather
    than accepted unvalidated. ``max_pixels``/``max_frames`` default to the
    configured settings when omitted.
    """
    try:
        from PIL import Image

        _HAS_PIL = True
    except ImportError:  # pragma: no cover - environment dependent
        _HAS_PIL = False

    if not _HAS_PIL:
        return False, IMAGE_ERROR_MISSING_LIBRARY

    if max_pixels is None or max_frames is None:
        try:
            from app.config import settings

            if max_pixels is None:
                max_pixels = settings.max_asset_pixels
            if max_frames is None:
                max_frames = settings.max_asset_frames
        except Exception:  # pragma: no cover - settings import failure
            if max_pixels is None:
                max_pixels = 50_000_000
            if max_frames is None:
                max_frames = 64

    str_path = str(path)
    try:
        with open(str_path, "rb") as fh:
            header = fh.read(32)
    except OSError:
        return False, IMAGE_ERROR_INVALID

    if _detect_image_reject_header(header):
        return False, IMAGE_ERROR_UNSUPPORTED

    try:
        with Image.open(str_path) as img:
            width = getattr(img, "width", 0) or 0
            height = getattr(img, "height", 0) or 0
            if width and height and width * height > max_pixels:
                return False, IMAGE_ERROR_DECOMPRESSION_BOMB
            frames = getattr(img, "n_frames", 1) or 1
            if frames > max_frames:
                return False, IMAGE_ERROR_TOO_MANY_FRAMES
            # Force a real decode so a corrupt/truncated payload is rejected.
            img.load()
    except Exception:
        return False, IMAGE_ERROR_INVALID

    return True, None


def secure_filename(filename: str) -> str:
    """
    Sanitize a filename to prevent security issues.

    - Strips paths using os.path.basename
    - Removes non-ASCII characters
    - Replaces spaces with underscores
    - Allows only alphanumeric, dots, hyphens, and underscores
    """
    # Strip paths
    filename = os.path.basename(filename)

    # Replace spaces with underscores
    filename = filename.replace(" ", "_")

    # Remove non-ASCII characters
    filename = filename.encode("ascii", "ignore").decode("ascii")

    # Allow only alphanumeric, dots, hyphens, and underscores
    filename = re.sub(r"[^a-zA-Z0-9._-]", "", filename)

    return filename
