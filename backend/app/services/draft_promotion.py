"""Draft Room promotion — the shared internal ingestion seam (issue #437,
``specs/draft-room/SPEC.md`` sections 3.4 and 6.4).

Promoting a draft input or revision copies its bytes into a vault's normal
upload directory and runs them through the *exact same* duplicate-check /
``files``-row-creation / background-enqueue sequence a normal document
upload uses (``app.api.routes.documents._do_upload``), so the promoted
document enters ordinary ingestion and indexing with no special-cased
behavior.

This module never imports from ``app.api.routes.*`` and never issues an HTTP
request — SPEC section 6.4 requires promotion to call this service directly,
not the upload route. It also never mutates the source ``draft_inputs`` /
``draft_revisions`` row and never writes to or references the private draft
input storage path (``DraftInputStorage``'s root); the destination path is
always inside ``UploadPathProvider().get_upload_dir(vault_id)``, and the
source input's bytes are only *read*, then copied.

Atomicity: a promotion either leaves behind bytes + a ``files`` row + a
``draft_promotions`` row + an enqueued ingestion job, or it leaves nothing at
all. ``DocumentProcessor._insert_or_get_file_record`` commits its own
transaction internally (shared with the normal upload route — not something
this module can safely change), so a single all-or-nothing SQL transaction
spanning the ``files`` insert and the ``draft_promotions`` insert is not
available without touching that shared method. Instead, every step after the
``files`` row is created runs inside one compensation block: if the
provenance insert, the phase update, or the enqueue call fails for any
reason, the just-created ``files`` row (and, if it was created, the
``draft_promotions`` row) are deleted along with the copied bytes, so a
failed promotion is never left half-done — see :func:`_promote`.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import settings
from app.models.database import SQLiteConnectionPool
from app.services.background_tasks import BackgroundProcessor
from app.services.document_processor import DocumentProcessor, DuplicateFileError
from app.services.document_progress import PHASE_QUEUED, set_phase
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_store import DraftInputRecord, DraftRevisionRecord
from app.services.upload_path import UploadPathProvider
from app.services.upload_validation import secure_filename
from app.utils.file_utils import compute_file_hash

logger = logging.getLogger(__name__)

#: ``files.source`` label for promoted documents (distinct from 'upload' /
#: 'scan' / 'email' — the column is free text, not an enforced enum).
PROMOTE_SOURCE = "draft_room_promote"

_FALLBACK_STEM = "promoted_document"

#: Conservative ceiling on the sanitized filename *stem*, well under typical
#: filesystem NAME_MAX (255 bytes), leaving headroom for the extension and
#: the collision loop's "_NNN" suffix. `PromoteRequest.title` allows up to
#: 300 characters; without this clamp, a long-but-otherwise-valid title can
#: make `os.open` raise `OSError: [Errno 36] File name too long`.
_MAX_FILENAME_STEM_CHARS = 200


class DraftPromotionError(Exception):
    """Base class for promotion-service failures."""

    code = "internal_error"


class DraftPromotionDuplicateError(DraftPromotionError):
    """Identical content already exists (pending/processing/indexed) in the
    destination vault. Carries the conflicting row's ``files.id`` when it is
    known; ``None`` when it could not be determined (never a fabricated id)."""

    code = "duplicate_document"

    def __init__(self, message: str, *, existing_file_id: Optional[int] = None) -> None:
        super().__init__(message)
        self.existing_file_id = existing_file_id


class DraftPromotionTooLargeError(DraftPromotionError):
    """The destination bytes exceed ``settings.max_file_size_mb``. Callers
    translate this to ``413``, matching ``_do_upload``'s own limit."""

    code = "promotion_too_large"


@dataclass
class DraftPromotionResult:
    """Everything the route needs to build ``PromoteResponse``."""

    promotion_id: int
    draft_id: int
    vault_id: int
    source_type: str
    source_id: int
    source_sha256: str
    file_id: int
    filename: str
    created_at: str


def _destination_filename(title: str, extension: str) -> str:
    stem = secure_filename(title) or _FALLBACK_STEM
    stem = stem[:_MAX_FILENAME_STEM_CHARS] or _FALLBACK_STEM
    return f"{stem}{extension}"


def _reserve_destination_path(upload_dir: Path, file_name: str) -> Path:
    """Atomically reserve a non-colliding path for ``file_name`` in ``upload_dir``.

    Mirrors ``app.api.routes.documents._do_upload`` on both fronts it
    combines: the O_CREAT|O_EXCL rename loop (never overwrites an existing
    file, appends ``_1``, ``_2``, ... on collision) and the post-reservation
    containment re-check. The loop catches ``OSError`` generally rather than
    only ``FileExistsError`` and re-raises immediately for anything that is
    not ``EEXIST`` (e.g. a filesystem-imposed name-length error), so a
    non-collision failure is never mistaken for one and retried into an even
    longer, still-failing name.
    """
    upload_dir.mkdir(parents=True, exist_ok=True)
    original_path = upload_dir / file_name
    file_path = original_path
    counter = 0
    while True:
        try:
            fd = os.open(str(file_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            counter += 1
            file_path = upload_dir / f"{original_path.stem}_{counter}{original_path.suffix}"

    # Defense-in-depth containment re-check (documents.py:1673-1685). The
    # collision loop only ever appends inside `upload_dir`, so no traversal
    # is actually reachable here, but this module's own docstring claims to
    # mirror `_do_upload` and should not silently drop one of its guards.
    try:
        resolved_path = file_path.resolve()
        resolved_upload_dir = upload_dir.resolve()
        if not str(resolved_path).startswith(str(resolved_upload_dir)):
            file_path.unlink(missing_ok=True)
            raise DraftPromotionError("resolved destination path escapes the upload directory")
    except DraftPromotionError:
        raise
    except (OSError, ValueError) as exc:
        file_path.unlink(missing_ok=True)
        raise DraftPromotionError("failed to validate destination path containment") from exc

    return file_path


def _copy_input_bytes(storage: DraftInputStorage, input_record: DraftInputRecord, dest: Path) -> None:
    source_path = storage.resolve(input_record.storage_relpath)
    shutil.copy2(source_path, dest)


def _write_revision_markdown(revision_record: DraftRevisionRecord, dest: Path) -> None:
    dest.write_bytes((revision_record.content_md or "").encode("utf-8"))


def _enforce_size_limit(dest_path: Path) -> None:
    """Reject a promoted document larger than ``settings.max_file_size_mb``.

    The input path is already safe — a draft input's bytes were capped at
    upload time by ``DraftInputStorage.stage_upload`` — so this exists for
    the revision path: ``RevisionCreateRequest.content_md`` has no practical
    upper bound enforced anywhere else that guarantees the rendered ``.md``
    stays under the vault's own ingestion limit.
    """
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if dest_path.stat().st_size > max_bytes:
        raise DraftPromotionTooLargeError(
            f"promoted document exceeds the {settings.max_file_size_mb} MB size limit"
        )


def _register_file(
    db_pool: SQLiteConnectionPool, dest_path: Path, file_hash: str, vault_id: int
) -> int:
    """Duplicate-check + ``files`` row creation, exactly like a normal upload.

    ``DocumentProcessor._insert_or_get_file_record`` commits its own
    transaction internally, so this function's write is already durable by
    the time it returns — see the module docstring for why the caller must
    treat every step after this one as needing its own compensation on
    failure, rather than assuming this can be folded into one larger
    transaction with the ``draft_promotions`` insert.
    """
    processor = DocumentProcessor(pool=db_pool)
    conn = db_pool.get_connection()
    try:
        duplicate = processor._check_duplicate_in_flight(file_hash, conn, vault_id)
        if duplicate is not None:
            raise DraftPromotionDuplicateError(
                f"file with hash {file_hash} already exists in this vault "
                f"(status={duplicate['status']}, file_id={duplicate['id']})",
                existing_file_id=int(duplicate["id"]),
            )
        try:
            file_id = processor._insert_or_get_file_record(
                str(dest_path), file_hash, conn, vault_id, PROMOTE_SOURCE, None, None
            )
        except DuplicateFileError as exc:
            # A race lost against another request's commit between the check
            # above and this insert. Re-query so the client-facing error
            # carries the real conflicting file id rather than a placeholder.
            conflict = processor._check_duplicate_in_flight(file_hash, conn, vault_id)
            existing_id = int(conflict["id"]) if conflict is not None else None
            raise DraftPromotionDuplicateError(str(exc), existing_file_id=existing_id) from exc
        conn.commit()
        return file_id
    finally:
        db_pool.release_connection(conn)


def _insert_promotion_row(
    db_pool: SQLiteConnectionPool,
    *,
    draft_id: int,
    source_type: str,
    source_id: int,
    source_sha256: str,
    vault_id: int,
    file_id: int,
    filename: str,
    promoted_by: int,
) -> tuple[int, str]:
    conn = db_pool.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO draft_promotions "
            "(draft_id, source_type, source_id, source_sha256, vault_id, "
            "file_id, filename, promoted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                source_type,
                source_id,
                source_sha256,
                vault_id,
                file_id,
                filename,
                promoted_by,
            ),
        )
        promotion_id = int(cursor.lastrowid)
        row = conn.execute(
            "SELECT created_at FROM draft_promotions WHERE id = ?", (promotion_id,)
        ).fetchone()
        conn.commit()
        return promotion_id, str(row["created_at"])
    except BaseException:
        conn.rollback()
        raise
    finally:
        db_pool.release_connection(conn)


def _delete_file_row(db_pool: SQLiteConnectionPool, file_id: int) -> None:
    """Compensation: remove a just-created ``files`` row when a later step in
    the same promotion attempt failed.

    Without this, a promotion that fails after the ``files`` row commits
    (provenance insert, phase update, or enqueue) leaves a permanently
    'pending' phantom document in the vault — the bytes get deleted by
    :func:`_discard` but the row never transitions, since nothing will ever
    enqueue it. (``_do_upload`` has this exact hole for the same reason and
    is not changed here — this module can only close it for promotion.)
    """
    conn = db_pool.get_connection()
    try:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning(
            "draft_room_promote: failed to compensate files row id=%s after a "
            "later promotion step failed",
            file_id,
        )
    finally:
        db_pool.release_connection(conn)


def _delete_promotion_row(db_pool: SQLiteConnectionPool, promotion_id: int) -> None:
    """Compensation: remove a just-created ``draft_promotions`` row when a
    later step (phase update or enqueue) in the same attempt failed."""
    conn = db_pool.get_connection()
    try:
        conn.execute("DELETE FROM draft_promotions WHERE id = ?", (promotion_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning(
            "draft_room_promote: failed to compensate draft_promotions row "
            "id=%s after a later promotion step failed",
            promotion_id,
        )
    finally:
        db_pool.release_connection(conn)


async def promote_input(
    *,
    storage: DraftInputStorage,
    db_pool: SQLiteConnectionPool,
    background_processor: BackgroundProcessor,
    draft_id: int,
    vault_id: int,
    title: str,
    promoted_by: int,
    input_record: DraftInputRecord,
) -> DraftPromotionResult:
    """Promote a draft input: copy its stored bytes into the vault, unmodified."""
    return await _promote(
        storage=storage,
        db_pool=db_pool,
        background_processor=background_processor,
        draft_id=draft_id,
        vault_id=vault_id,
        title=title,
        promoted_by=promoted_by,
        source_type="input",
        source_id=input_record.id,
        extension=input_record.extension,
        write_bytes=lambda dest: _copy_input_bytes(storage, input_record, dest),
    )


async def promote_revision(
    *,
    storage: DraftInputStorage,
    db_pool: SQLiteConnectionPool,
    background_processor: BackgroundProcessor,
    draft_id: int,
    vault_id: int,
    title: str,
    promoted_by: int,
    revision_record: DraftRevisionRecord,
) -> DraftPromotionResult:
    """Promote a draft revision: render its exact ``content_md`` as a ``.md`` file."""
    return await _promote(
        storage=storage,
        db_pool=db_pool,
        background_processor=background_processor,
        draft_id=draft_id,
        vault_id=vault_id,
        title=title,
        promoted_by=promoted_by,
        source_type="revision",
        source_id=revision_record.id,
        extension=".md",
        write_bytes=lambda dest: _write_revision_markdown(revision_record, dest),
    )


async def _promote(
    *,
    storage: DraftInputStorage,
    db_pool: SQLiteConnectionPool,
    background_processor: BackgroundProcessor,
    draft_id: int,
    vault_id: int,
    title: str,
    promoted_by: int,
    source_type: str,
    source_id: int,
    extension: str,
    write_bytes,
) -> DraftPromotionResult:
    upload_dir = UploadPathProvider().get_upload_dir(vault_id)
    file_name = _destination_filename(title, extension)
    dest_path = await asyncio.to_thread(_reserve_destination_path, upload_dir, file_name)

    file_id: Optional[int] = None
    promotion_id: Optional[int] = None
    try:
        await asyncio.to_thread(write_bytes, dest_path)
        await asyncio.to_thread(_enforce_size_limit, dest_path)
        source_sha256 = await asyncio.to_thread(compute_file_hash, str(dest_path))
        file_id = await asyncio.to_thread(_register_file, db_pool, dest_path, source_sha256, vault_id)
        promotion_id, created_at = await asyncio.to_thread(
            _insert_promotion_row,
            db_pool,
            draft_id=draft_id,
            source_type=source_type,
            source_id=source_id,
            source_sha256=source_sha256,
            vault_id=vault_id,
            file_id=file_id,
            filename=dest_path.name,
            promoted_by=promoted_by,
        )
        await asyncio.to_thread(
            set_phase,
            db_pool,
            file_id,
            phase=PHASE_QUEUED,
            message="Queued for processing",
        )
        await background_processor.enqueue(
            file_path=str(dest_path),
            source=PROMOTE_SOURCE,
            vault_id=vault_id,
            file_id=file_id,
        )
    except BaseException as exc:
        # Full rollback of everything this attempt created, in reverse order:
        # a promotion either fully happened or did not happen at all. Never
        # report failure while leaving the document, its provenance row, or
        # its bytes behind — a client retry must be safe, and the audit
        # trail must never lie about what exists.
        if promotion_id is not None:
            await asyncio.to_thread(_delete_promotion_row, db_pool, promotion_id)
        if file_id is not None:
            await asyncio.to_thread(_delete_file_row, db_pool, file_id)
        await asyncio.to_thread(_discard, dest_path)
        # Recognized domain errors (duplicate / too-large) carry a specific
        # meaning the route maps to a specific status code and must pass
        # through unchanged. Anything else — a bare `sqlite3.IntegrityError`
        # from a concurrently deleted draft, an `OSError`, etc. — is wrapped
        # into a generic `DraftPromotionError` so the route's catch-all
        # handler returns the same clean error envelope every other failure
        # here does, instead of leaking a raw, unhandled exception type.
        if isinstance(exc, DraftPromotionError):
            raise
        raise DraftPromotionError(f"promotion failed: {exc}") from exc

    return DraftPromotionResult(
        promotion_id=promotion_id,
        draft_id=draft_id,
        vault_id=vault_id,
        source_type=source_type,
        source_id=source_id,
        source_sha256=source_sha256,
        file_id=file_id,
        filename=dest_path.name,
        created_at=created_at,
    )


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)
