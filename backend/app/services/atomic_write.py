"""Canonical temp-sibling helpers for writes into FileWatcher-scanned directories.

Every producer covered by this change (document upload, draft-promotion
input/revision copy, and startup ``migrate_uploads``) that places bytes at a
final path inside a directory ``FileWatcher._find_new_files`` scans (each
vault's uploads directory and the optional library directory) writes to a temp
sibling first and ``os.replace``s it onto the final path only after the bytes
are complete and validated — the same contract as
``DraftInputStorage.stage_upload`` and ``artifact_store._atomic_write_bytes``.
A kill mid-write then leaves a canonical temp artifact instead of a truncated
document, and ``FileWatcher`` skips canonical temp artifacts by shape
(issue #786).

This module deliberately matches ONLY the exact shape it generates:
``.{name}.{32-hex-uuid}.part``. Other in-repo temp producers in watched
directories are NOT matched and keep their pre-existing scan behavior — the
email poller's ``email_<random><ext>`` mkstemp attachments (email_service.py;
out of this issue's named scope) are the known example. ``artifact_store``'s
``.tmp-asset-*`` files live in non-scanned directories.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

# Exact generated shape: a leading dot, the original file name, a dot,
# uuid4().hex (32 lowercase hex chars), then ".part". Matching the generator
# exactly — rather than a loose prefix/suffix rule — is what lets FileWatcher
# skip these artifacts without ever silently dropping a legitimate user file
# such as a library-dir ".notes.part".
_TEMP_PATTERN = re.compile(r"^\..+\.[0-9a-f]{32}\.part$")

_TEMP_SUFFIX = ".part"

# The temp leaf name is ``.{name}.{32 hex}.{suffix}``, so it is 39 characters
# longer than the destination name. Most filesystems cap a single path
# component at 255 bytes, so a destination name that the product already
# accepts can be 217-255 characters long — long enough that adding the temp
# affix would exceed that cap and fail the write with ENAMETOOLONG (issue
# #886 review PRR-005). Truncating ONLY the affix-carrying stem inside the temp
# name keeps every destination name the product accepts, while the temp stays
# unique (the uuid) and still matches ``_TEMP_PATTERN``.
_MAX_NAME_COMPONENT = 255
_TEMP_AFFIX_LEN = 1 + 1 + 32 + len(_TEMP_SUFFIX)


def temp_sibling(dest: Path) -> Path:
    """Return a same-directory canonical temp path for writing ``dest``.

    The temp file must be a sibling of ``dest`` (same directory) so the final
    ``os.replace`` is an atomic same-volume rename. The destination's own name
    is clamped so the temp never exceeds the filesystem's per-component limit;
    ``dest`` itself is never renamed or truncated.
    """
    stem = dest.name
    max_stem = _MAX_NAME_COMPONENT - _TEMP_AFFIX_LEN
    if len(stem) > max_stem:
        stem = stem[:max_stem]
    return dest.parent / f".{stem}.{uuid4().hex}{_TEMP_SUFFIX}"


def is_temp_artifact(path: Path) -> bool:
    """True only when ``path``'s name matches the canonical generated shape."""
    return bool(_TEMP_PATTERN.match(path.name))
