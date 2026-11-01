"""Filesystem storage for Draft Room raw inputs (issue #435, SPEC section 6.1/6.2).

Owns every byte-level operation on ``data/draft-room/``: streaming an upload
into a validated temp file, atomically finalizing it at its canonical path,
resolving a stored relative path back to a filesystem path without ever
escaping the root, and the two-phase tombstone rename/restore/commit
primitives that back durable deletion.

This module never touches ``draft_store``/SQLite and never calls
``DocumentExtractionService`` — it is pure storage plumbing. Nothing here logs
manuscript text, absolute paths, client filenames, or ``repr(exc)``; only IDs
and stable codes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.services.upload_validation import (
    _OOXML_REQUIRED_MEMBERS,
    _check_magic_bytes,
    _validate_ooxml_member,
    secure_filename,
)

logger = logging.getLogger(__name__)

_INCOMING_DIR = ".incoming"
_TRASH_DIR = ".trash"
_STREAM_CHUNK_BYTES = 1024 * 1024
_HEADER_BYTES = 8


@dataclass
class StagedUpload:
    """A validated upload sitting in ``.incoming``, not yet at its final path."""

    temp_path: Path
    size_bytes: int
    content_sha256: str
    extension: str
    stored_name: str
    original_name: str
    media_type: Optional[str]


class DraftInputStorageError(Exception):
    """Base class for Draft Room filesystem failures."""

    code = "internal_error"


class DraftInputTooLargeError(DraftInputStorageError):
    """Upload exceeded the configured byte limit. Callers translate to ``413``."""

    code = "input_too_large"


class DraftInputUnsupportedError(DraftInputStorageError):
    """Extension or content signature is unsupported. Callers translate to ``415``."""

    code = "unsupported_input"


class DraftInputPathError(DraftInputStorageError):
    """A relative path failed to resolve safely inside the storage root."""

    code = "invalid_storage_path"


def _reject_unsafe_relpath(relpath: str) -> None:
    """Reject the obviously unsafe shapes *before* any filesystem resolution.

    Fails closed on empty/absolute paths and any ``.``/``..`` component so a
    traversal attempt never reaches ``Path.resolve()``. Backslashes are treated
    as separators too, so a Windows-style traversal is caught on any platform.
    """
    if not relpath:
        raise DraftInputPathError("empty storage path")
    if relpath.startswith("/") or relpath.startswith("\\") or os.path.isabs(relpath):
        raise DraftInputPathError("absolute storage path")
    for part in relpath.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            raise DraftInputPathError("invalid storage path component")


class DraftInputStorage:
    """Byte-level Draft Room storage rooted at ``settings.data_dir / 'draft-room'``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    # ── path resolution ─────────────────────────────────────────────────

    def resolve(self, relpath: str) -> Path:
        """Resolve ``relpath`` against the root, failing closed on any escape.

        Rejects ``..``, absolute paths, and symlink/reparse-point escape by
        resolving both the root and the candidate target and verifying the
        target is still inside the resolved root.
        """
        _reject_unsafe_relpath(relpath)
        try:
            resolved_root = self._root.resolve()
            candidate = (resolved_root / relpath).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise DraftInputPathError("storage path could not be resolved") from exc
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise DraftInputPathError("storage path escapes storage root") from exc
        return candidate

    def exists(self, relpath: str) -> bool:
        return self.resolve(relpath).is_file()

    def read_text(self, relpath: str) -> str:
        path = self.resolve(relpath)
        return path.read_text(encoding="utf-8")

    # ── staging ──────────────────────────────────────────────────────────

    async def stage_upload(
        self,
        upload,
        *,
        allowed_extensions: set[str],
        max_file_bytes: int,
    ) -> StagedUpload:
        """Stream ``upload`` into ``.incoming`` while validating it in bounded chunks.

        Never buffers the full body in memory: size and SHA-256 are computed
        incrementally, and the write aborts (deleting the partial file) the
        moment ``max_file_bytes`` is exceeded.

        Raises:
            DraftInputUnsupportedError: Extension/magic-bytes/OOXML mismatch.
            DraftInputTooLargeError: The stream exceeded ``max_file_bytes``.
        """
        original_name = secure_filename(upload.filename or "unnamed_file")
        if not original_name:
            original_name = "unnamed_file"
        extension = Path(original_name).suffix.lower()
        if extension not in allowed_extensions:
            raise DraftInputUnsupportedError("extension not allowed")

        incoming_dir = self._root / _INCOMING_DIR
        incoming_dir.mkdir(parents=True, exist_ok=True)
        temp_path = incoming_dir / f"{uuid.uuid4().hex}.part"

        hasher = hashlib.sha256()
        total_bytes = 0
        header_bytes = b""
        try:
            with open(temp_path, "wb") as f:
                header_bytes = await upload.read(_HEADER_BYTES)
                if header_bytes:
                    if not _check_magic_bytes(extension, header_bytes):
                        raise DraftInputUnsupportedError(
                            "content does not match declared extension"
                        )
                    hasher.update(header_bytes)
                    total_bytes += len(header_bytes)
                    f.write(header_bytes)
                    if total_bytes > max_file_bytes:
                        raise DraftInputTooLargeError("upload exceeds size limit")

                while True:
                    chunk = await upload.read(_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_file_bytes:
                        raise DraftInputTooLargeError("upload exceeds size limit")
                    hasher.update(chunk)
                    f.write(chunk)

            required_member = _OOXML_REQUIRED_MEMBERS.get(extension)
            if required_member is not None:
                if not _validate_ooxml_member(temp_path, required_member):
                    raise DraftInputUnsupportedError(
                        "archive is missing required member for its extension"
                    )
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        media_type = _guess_media_type(extension)
        stored_name = f"{uuid.uuid4().hex}{extension}"
        return StagedUpload(
            temp_path=temp_path,
            size_bytes=total_bytes,
            content_sha256=hasher.hexdigest(),
            extension=extension,
            stored_name=stored_name,
            original_name=original_name,
            media_type=media_type,
        )

    def discard(self, staged: StagedUpload) -> None:
        """Delete a staged upload that will not be finalized."""
        staged.temp_path.unlink(missing_ok=True)

    def finalize(self, staged: StagedUpload, relpath: str) -> None:
        """Atomically move a staged upload to its canonical final path."""
        final_path = self.resolve(relpath)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.temp_path, final_path)

    # ── two-phase tombstone deletion ────────────────────────────────────

    def tombstone(self, relpath: str) -> str:
        """Atomically rename the resolved path into ``.trash`` and return its token.

        Issue #723 (T1-21-S2-03): the tombstone's mtime is refreshed to NOW
        (``os.replace`` preserves the upload's old mtime, which made the
        startup reconcile delete a surviving draft's tombstoned bytes purely
        on upload age), and a ``<token>.json`` sidecar records the owning
        relpath so a restart can restore tombstones whose drafts still
        exist.

        Raises:
            DraftInputPathError: The storage root itself is missing. Creating
                it here would relocate the user's bytes into a fresh tree while
                the caller reads the resulting ``FileNotFoundError`` as "bytes
                are gone", so this fails closed instead.
            FileNotFoundError: The source file does not exist, which callers may
                tolerate as "bytes are absent". A missing PARENT directory is
                deliberately not treated this way: SPEC 6.3/AC5 requires an
                input to stay deletable after startup reconcile removes its
                project directory, so the two states are indistinguishable
                here and the tolerant reading is kept.
        """
        if not self._root.is_dir():
            raise DraftInputPathError("draft storage root is missing")
        source = self.resolve(relpath)
        trash_dir = self._root / _TRASH_DIR
        trash_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        trash_path = trash_dir / token
        # Issue #723 review (F-1): write the sidecar BEFORE the rename so a
        # failure in the metadata write cannot strand a sidecar-less entry
        # (the restore pass skips those and the age purge would destroy the
        # only copy of a surviving draft's bytes). If the rename itself
        # fails, unlink the sidecar and re-raise with the bytes untouched at
        # their original path; an orphan sidecar without an entry is cleaned
        # by the reconcile's restore pass.
        self._sidecar_path(token).write_text(
            json.dumps({"relpath": relpath}), encoding="utf-8"
        )
        try:
            os.replace(source, trash_path)
        except OSError:
            self._sidecar_path(token).unlink(missing_ok=True)
            raise
        os.utime(trash_path, None)
        return token

    def restore_tombstone(self, token: str, relpath: str) -> None:
        """Rename a tombstoned file back to its original relative path."""
        trash_path = self._trash_path(token)
        final_path = self.resolve(relpath)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(trash_path, final_path)
        self._sidecar_path(token).unlink(missing_ok=True)

    def commit_tombstone(self, token: str) -> None:
        """Permanently discard a tombstoned file."""
        self._trash_path(token).unlink(missing_ok=True)
        self._sidecar_path(token).unlink(missing_ok=True)

    def _trash_path(self, token: str) -> Path:
        if not token or any(c in token for c in ("/", "\\", "..")):
            raise DraftInputPathError("invalid tombstone token")
        return (self._root / _TRASH_DIR / token).resolve()

    def _sidecar_path(self, token: str) -> Path:
        # Same validation as _trash_path; the sidecar rides beside the entry.
        if not token or any(c in token for c in ("/", "\\", "..")):
            raise DraftInputPathError("invalid tombstone token")
        return (self._root / _TRASH_DIR / f"{token}.json").resolve()

    # ── startup reconciliation ──────────────────────────────────────────

    def reconcile(
        self,
        valid_owner_draft_pairs: set[tuple[int, int]],
        *,
        max_age_seconds: float,
        valid_input_relpaths: Optional[set[str]] = None,
    ) -> dict[str, int]:
        """Remove stale ``.incoming``/``.trash`` entries and orphan project dirs.

        Runs at startup before HTTP traffic/workers. Age-based cleanup applies
        only to ``.incoming``/``.trash``; ``<user_id>/<draft_id>`` directories
        whose pair is not in ``valid_owner_draft_pairs`` are removed immediately
        regardless of age.

        Issue #723 (T1-21-S2-03): before the age purge, every sidecar-bearing
        ``.trash`` entry whose bytes are still owed to a live row is restored.
        When ``valid_input_relpaths`` is provided (the exact rule — the
        production startup passes the DB-derived set of ``draft_inputs``.storage_relpath
        values), only relpaths still backed by a row are restored; with the
        legacy pairs-only call shape, entries whose relpath's ``(owner, draft)``
        pair is valid are restored (a coarser fallback that can also restore a
        concurrently-deleted input's bytes — the shipped startup path never
        uses it). Entries whose target relpath already exists are never
        restored and age-purge as redundant bytes.
        """
        counts = {
            "incoming_removed": 0,
            "trash_removed": 0,
            "trash_restored": 0,
            "orphan_dirs_removed": 0,
        }
        if not self._root.is_dir():
            return counts

        counts["trash_restored"] = self._reconcile_restore_trash(
            valid_owner_draft_pairs, valid_input_relpaths
        )
        now = time.time()
        counts["incoming_removed"] = self._reconcile_age_dir(
            self._root / _INCOMING_DIR, now=now, max_age_seconds=max_age_seconds
        )
        counts["trash_removed"] = self._reconcile_age_dir(
            self._root / _TRASH_DIR,
            now=now,
            max_age_seconds=max_age_seconds,
            sidecar_suffix=".json",
        )
        counts["orphan_dirs_removed"] = self._reconcile_orphan_projects(
            valid_owner_draft_pairs
        )
        return counts

    def _reconcile_restore_trash(
        self,
        valid_owner_draft_pairs: set[tuple[int, int]],
        valid_input_relpaths: Optional[set[str]],
    ) -> int:
        """Restore tombstoned bytes still owed to a live draft-input row.

        Sidecars without an entry are orphaned metadata and removed; entries
        without a sidecar (pre-fix tombstones) are left to the age purge.
        """
        trash_dir = self._root / _TRASH_DIR
        if not trash_dir.is_dir():
            return 0
        restored = 0
        for sidecar in list(trash_dir.glob("*.json")):
            token = sidecar.name[: -len(".json")]
            entry = trash_dir / token
            if not entry.exists():
                sidecar.unlink(missing_ok=True)
                continue
            try:
                relpath = json.loads(sidecar.read_text(encoding="utf-8"))["relpath"]
            except (OSError, ValueError, KeyError):
                continue
            if valid_input_relpaths is not None:
                owed = relpath in valid_input_relpaths
            else:
                pair = _owner_draft_pair_from_relpath(relpath)
                owed = pair is not None and pair in valid_owner_draft_pairs
            if not owed or self.exists(relpath):
                continue
            try:
                os.replace(entry, self.resolve(relpath))
                sidecar.unlink(missing_ok=True)
                restored += 1
            except (OSError, DraftInputPathError):
                logger.warning(
                    "draft_room_reconcile: failed to restore tombstoned input"
                )
        return restored

    def _reconcile_age_dir(
        self,
        directory: Path,
        *,
        now: float,
        max_age_seconds: float,
        sidecar_suffix: Optional[str] = None,
    ) -> int:
        if not directory.is_dir():
            return 0
        removed = 0
        try:
            resolved_dir = directory.resolve()
            resolved_dir.relative_to(self._root.resolve())
        except (OSError, ValueError):
            return 0
        for entry in directory.iterdir():
            if sidecar_suffix and entry.name.endswith(sidecar_suffix):
                # Sidecar metadata is paired with its entry — never counted
                # or purged as an independent entry; it is unlinked alongside
                # its entry below (or as an orphan by the restore pass).
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < max_age_seconds:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    import shutil

                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                if sidecar_suffix:
                    entry.with_name(entry.name + sidecar_suffix).unlink(
                        missing_ok=True
                    )
                removed += 1
            except OSError:
                logger.warning("draft_room_reconcile: failed to remove stale entry")
        return removed

    def _reconcile_orphan_projects(
        self, valid_owner_draft_pairs: set[tuple[int, int]]
    ) -> int:
        if not self._root.is_dir():
            return 0
        removed = 0
        resolved_root = self._root.resolve()
        for owner_entry in self._root.iterdir():
            if owner_entry.name in (_INCOMING_DIR, _TRASH_DIR):
                continue
            if not owner_entry.is_dir() or not owner_entry.name.isdigit():
                continue
            owner_id = int(owner_entry.name)
            for draft_entry in owner_entry.iterdir():
                if not draft_entry.is_dir() or not draft_entry.name.isdigit():
                    continue
                draft_id = int(draft_entry.name)
                if (owner_id, draft_id) in valid_owner_draft_pairs:
                    continue
                try:
                    resolved_entry = draft_entry.resolve()
                    resolved_entry.relative_to(resolved_root)
                except (OSError, ValueError):
                    continue
                import shutil

                shutil.rmtree(resolved_entry, ignore_errors=True)
                removed += 1
        return removed


def _owner_draft_pair_from_relpath(relpath: str) -> Optional[tuple[int, int]]:
    """Best-effort ``(owner_id, draft_id)`` from a storage relpath.

    Layout is ``<owner_id>/<draft_id>/inputs/<input_id>/<name>``; returns None
    when the leading segments are not both numeric.
    """
    parts = relpath.split("/")
    if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
        return (int(parts[0]), int(parts[1]))
    return None


def _guess_media_type(extension: str) -> Optional[str]:
    import mimetypes

    media_type, _ = mimetypes.guess_type(f"file{extension}")
    return media_type
