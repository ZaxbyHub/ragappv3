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
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

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


class DraftPromotionError(Exception):
    """Base class for promotion-service failures."""

    code = "internal_error"


class DraftPromotionDuplicateError(DraftPromotionError):
    """Identical content already exists (pending/processing/indexed) in the
    destination vault. Carries the conflicting row's ``files.id``."""

    code = "duplicate_document"

    def __init__(self, message: str, *, existing_file_id: int) -> None:
        super().__init__(message)
        self.existing_file_id = existing_file_id


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
    return f"{stem}{extension}"


def _reserve_destination_path(upload_dir: Path, file_name: str) -> Path:
    """Atomically reserve a non-colliding path for ``file_name`` in ``upload_dir``.

    Mirrors the O_CREAT|O_EXCL rename loop in
    ``app.api.routes.documents._do_upload`` exactly: never overwrites an
    existing file, appends ``_1``, ``_2``, ... on collision.
    """
    upload_dir.mkdir(parents=True, exist_ok=True)
    original_path = upload_dir / file_name
    file_path = original_path
    counter = 0
    while True:
        try:
            fd = os.open(str(file_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return file_path
        except FileExistsError:
            counter += 1
            file_path = upload_dir / f"{original_path.stem}_{counter}{original_path.suffix}"


def _copy_input_bytes(storage: DraftInputStorage, input_record: DraftInputRecord, dest: Path) -> None:
    source_path = storage.resolve(input_record.storage_relpath)
    shutil.copy2(source_path, dest)


def _write_revision_markdown(revision_record: DraftRevisionRecord, dest: Path) -> None:
    dest.write_bytes((revision_record.content_md or "").encode("utf-8"))


def _register_file(
    db_pool: SQLiteConnectionPool, dest_path: Path, file_hash: str, vault_id: int
) -> int:
    """Duplicate-check + ``files`` row creation, exactly like a normal upload."""
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
            raise DraftPromotionDuplicateError(
                str(exc), existing_file_id=-1
            ) from exc
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

    try:
        await asyncio.to_thread(write_bytes, dest_path)
        source_sha256 = await asyncio.to_thread(compute_file_hash, str(dest_path))
        file_id = await asyncio.to_thread(_register_file, db_pool, dest_path, source_sha256, vault_id)
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
    except BaseException:
        await asyncio.to_thread(_discard, dest_path)
        raise

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
