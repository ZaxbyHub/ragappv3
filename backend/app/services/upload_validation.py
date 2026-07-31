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
