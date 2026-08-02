"""Draft Room promotion — the shared internal ingestion seam (issue #437,
``specs/draft-room/SPEC.md`` sections 3.4 and 6.4).

Promoting a draft input or revision copies its bytes into a vault's normal
upload directory and creates a ``files`` row + enqueues it for background
ingestion, the same downstream effect a normal document upload
(``app.api.routes.documents._do_upload``) has, so the promoted document
enters ordinary ingestion and indexing with no special-cased behavior.

This module never imports from ``app.api.routes.*`` and never issues an HTTP
request — SPEC section 6.4 requires promotion to call this service directly,
not the upload route. It also never mutates the source ``draft_inputs`` /
``draft_revisions`` row and never writes to or references the private draft
input storage path (``DraftInputStorage``'s root); the destination path is
always inside ``UploadPathProvider().get_upload_dir(vault_id)``, and the
source input's bytes are only *read*, then copied.

Promotion does NOT call ``DocumentProcessor._insert_or_get_file_record`` (the
upsert ``_do_upload`` uses) — deliberately. That method's adoption branch is
an UPDATE: given an existing ``files`` row at the destination path (reachable
whenever an earlier row's bytes went missing without the row itself being
cleaned up, or from ``FileWatcher``'s default-on background scan of the same
upload directory landing in the window between reserving the path and
registering the row), it silently overwrites that row's ``file_hash`` /
``file_size`` / ``file_type`` / ``vault_id`` / ``source`` and forces
``status='pending'``. A promotion that later failed and tried to compensate
could not undo that overwrite — the victim document's identity is gone
either way, by UPDATE if adoption happens, or corrupted regardless of
whether the caller then also deletes the row. Instead, :func:`_register_file`
does its own exclusive insert inside one ``BEGIN IMMEDIATE`` transaction that
re-checks both the content hash and the destination path first: if identical
content already exists in the vault, or if anything already occupies that
exact path — no matter what created it, or when — this raises the
corresponding domain error rather than touching that row. ``created`` is
therefore true by construction for every ``files`` row this module ever
creates, and compensation can always safely delete exactly the rows (and
only the rows) this attempt itself made.

Atomicity: a promotion either leaves behind bytes + a ``files`` row + a
``draft_promotions`` row + an enqueued ingestion job, or it leaves nothing at
all. ``_register_file`` and ``_insert_promotion_row`` each commit their own
transaction (there is no cross-table atomicity between the two "files"
and "draft_promotions" inserts — see ``_insert_promotion_row``'s docstring
for why), so every step after the ``files`` row is created runs inside one
compensation block: if the provenance insert, the phase update, or the
enqueue call fails for any reason, the just-created ``files`` row (and, if it
was created, the ``draft_promotions`` row) are deleted along with the copied
bytes, so a failed promotion is never left half-done — see :func:`_promote`.
Compensation itself never raises (every step is independently guarded) and
runs shielded from cancellation with a bounded timeout, because Starlette
delivers a dropped client connection as anyio task-group cancellation, which
re-raises at the *next* await regardless of ordinary ``asyncio.shield`` —
without an anyio-level shield, a cancelled request would skip compensation
entirely and leave everything behind.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import anyio

from app.config import settings
from app.models.database import SQLiteConnectionPool
from app.services.background_tasks import BackgroundProcessor
from app.services.document_processor import DocumentProcessor
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

#: Upper bound on how long failure-path compensation (deleting whatever this
#: attempt itself created, plus the copied bytes) may run once shielded from
#: cancellation. Generous for three local SQLite statements and one unlink,
#: but finite: a genuinely wedged pool must not hang the request forever.
_COMPENSATION_TIMEOUT_SECONDS = 30.0


class DraftPromotionError(Exception):
    """Base class for promotion-service failures.

    ``cleanup_incomplete`` is set by :func:`_promote`'s compensation block
    when this failure occurred *after* the ``files`` row (and/or the
    ``draft_promotions`` row) had already been created, but the attempt to
    delete it back out again itself failed (e.g. ``database is locked``).
    That combination means an orphan may genuinely still exist — callers
    must record it somewhere an operator can find it and must not report it
    with the same, indistinguishable error as an ordinary clean failure.
    """

    code = "internal_error"

    def __init__(self, message: str, *, cleanup_incomplete: bool = False) -> None:
        super().__init__(message)
        self.cleanup_incomplete = cleanup_incomplete


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


class DraftPromotionPathConflictError(DraftPromotionError):
    """A ``files`` row already exists at the exact path this attempt reserved.

    Reachable when an earlier row's bytes went missing without the row
    itself being cleaned up, or when ``FileWatcher``'s default-on background
    scan (``settings.auto_scan_enabled``) inserts a row for this same path
    in the window between reserving it and registering it. Either way, this
    module never adopts or overwrites a row it did not itself insert —
    see the module docstring. Callers translate this to ``409``.
    """

    code = "promotion_path_conflict"


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


async def _reserve_destination_path_shielded(upload_dir: Path, file_name: str) -> Path:
    """Reserve the destination path shielded from cancellation.

    This is a fast, local filesystem call with a real, irreversible side
    effect (it creates an empty file on disk). If the ``await`` on it were
    cancelled at exactly this point, the underlying thread-pool worker would
    still create the file — threads cannot be forcibly stopped — but the
    caller would never learn the path, and so could never clean it up
    (nothing this module doesn't track can be discarded, and the file would
    become a ``FileWatcher`` scan target). Shielding guarantees the caller
    always ends up with a definite ``dest_path`` (or a definite failure
    before any file was created) instead of this in-between state.
    """
    with anyio.CancelScope(shield=True):
        return await asyncio.to_thread(_reserve_destination_path, upload_dir, file_name)


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


def _register_file(db_pool: SQLiteConnectionPool, dest_path: Path, file_hash: str, vault_id: int) -> int:
    """Promotion's own exclusive ``files`` row registration: the
    duplicate-content check, the path-conflict check, and the insert all run
    inside one ``BEGIN IMMEDIATE`` transaction on one connection.

    The duplicate-content check and the insert used to run on *separate*
    connections (the check released its connection before the path-checking
    insert transaction acquired its own) — two concurrent promotions of
    identical bytes to two different destination filenames could each pass
    the duplicate lookup before either had committed a row, and both would
    then insert, landing two ``files`` rows for the same content instead of
    the documented ``409 duplicate_document``. ``BEGIN IMMEDIATE`` takes
    SQLite's write lock before either check runs, so a second, concurrent
    caller genuinely blocks (``PRAGMA busy_timeout``) until the first
    transaction commits or rolls back, and then sees that transaction's
    effects — the race window is closed by construction, not by luck.

    Precedence when a row satisfies *both* checks (identical content already
    exists in the vault, AND something already occupies the exact
    destination path): the duplicate-content check runs first, so
    :class:`DraftPromotionDuplicateError` (``409 duplicate_document``) wins
    over :class:`DraftPromotionPathConflictError` (``409
    promotion_path_conflict``). "Identical content already exists" is judged
    the more actionable signal to the caller than "something unrelated
    occupies this path" when both happen to be true at once.

    Deliberately does NOT call ``DocumentProcessor._insert_or_get_file_record``
    (the path-keyed upsert ``_do_upload`` uses) — see the module docstring
    for why an adopted row can never be safely compensated. If anything
    already exists at the reserved path, this raises
    :class:`DraftPromotionPathConflictError` instead of adopting or
    overwriting it; the caller must clean up the reserved bytes and fail.
    The returned id always names a row this call itself just created, so the
    caller's compensation logic can always safely delete it on a later
    failure without risk of touching an unrelated document.

    The column set and defaults mirror ``_insert_or_get_file_record``'s own
    insert branch (``document_processor.py``) exactly — ``status='pending'``,
    the same five identity columns, ``created_at``/``modified_at`` stamped
    with the same UTC-ISO convention — so ingestion behaves identically
    either way. That method itself is not imported or modified here.
    """
    processor = DocumentProcessor(pool=db_pool)
    file_name = dest_path.name
    file_size = dest_path.stat().st_size
    file_type = dest_path.suffix.lower() if dest_path.suffix else None
    now = datetime.now(UTC).isoformat()
    path_str = str(dest_path)

    conn = db_pool.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            duplicate = processor._check_duplicate_in_flight(file_hash, conn, vault_id)
            if duplicate is not None:
                raise DraftPromotionDuplicateError(
                    f"file with hash {file_hash} already exists in this vault "
                    f"(status={duplicate['status']}, file_id={duplicate['id']})",
                    existing_file_id=int(duplicate["id"]),
                )
            existing = conn.execute(
                "SELECT id FROM files WHERE file_path = ?", (path_str,)
            ).fetchone()
            if existing is not None:
                raise DraftPromotionPathConflictError(
                    f"a files row already exists for path {path_str!r}"
                )
            cursor = conn.execute(
                "INSERT INTO files "
                "(file_path, file_name, file_hash, file_size, file_type, "
                "vault_id, source, status, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    path_str,
                    file_name,
                    file_hash,
                    file_size,
                    file_type,
                    vault_id,
                    PROMOTE_SOURCE,
                    now,
                    now,
                ),
            )
            new_file_id = cursor.lastrowid
            if new_file_id is None:
                raise DraftPromotionError("failed to insert files row: lastrowid is None")
            conn.commit()
            return int(new_file_id)
        except BaseException:
            conn.rollback()
            raise
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
    """Insert the provenance row and return ``(promotion_id, created_at)``.

    The insert-and-commit is isolated in its own inner ``try`` so that once
    it succeeds, ``promotion_id`` is guaranteed to reach the caller — a
    failure in the follow-up read-back of ``created_at`` (effectively
    unreachable in practice: it is the same row, on the same connection,
    immediately after commit) falls back to an empty string rather than
    raising, so the caller can never end up believing a row was never
    created when it actually committed. Without this, the caller's
    compensation logic would skip deleting a row that genuinely exists,
    leaving a permanent orphan with nothing left to reap it now that
    ``file_id`` is not a foreign key.
    """
    conn = db_pool.get_connection()
    try:
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
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        try:
            row = conn.execute(
                "SELECT created_at FROM draft_promotions WHERE id = ?", (promotion_id,)
            ).fetchone()
            created_at = str(row["created_at"]) if row is not None else ""
        except Exception:
            created_at = ""
        return promotion_id, created_at
    finally:
        db_pool.release_connection(conn)


def _delete_file_row(db_pool: SQLiteConnectionPool, file_id: int) -> bool:
    """Compensation: remove a ``files`` row this attempt itself inserted
    (see :func:`_register_file` — never an adopted or unrelated one)
    when a later step in the same promotion attempt failed.

    Without this, a promotion that fails after the ``files`` row commits
    (provenance insert, phase update, or enqueue) leaves a permanently
    'pending' phantom document in the vault — the bytes get deleted by
    :func:`_discard_safe` but the row never transitions, since nothing will
    ever enqueue it. (``_do_upload`` has this exact hole for the same reason
    and is not changed here — this module can only close it for promotion.)

    Connection acquisition is inside the ``try`` deliberately: pool
    exhaustion is exactly the condition most likely to have caused the
    original failure this is compensating for, so it must be at least as
    likely to fail here too. This function must never raise — every failure
    mode returns ``False`` instead, so it can never replace the exception
    :func:`_promote` is already handling with one of its own.
    """
    try:
        conn = db_pool.get_connection()
    except Exception as exc:
        logger.warning(
            "draft_room_promote: could not obtain a connection to compensate "
            "files row id=%s — an orphan files row may now exist: %s",
            file_id,
            exc,
        )
        return False
    try:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception as rollback_exc:
            logger.debug(
                "draft_room_promote: rollback failed while compensating files "
                "row id=%s: %s",
                file_id,
                rollback_exc,
            )
        logger.warning(
            "draft_room_promote: failed to compensate files row id=%s after a "
            "later promotion step failed — an orphan files row may now exist",
            file_id,
        )
        return False
    finally:
        try:
            db_pool.release_connection(conn)
        except Exception as release_exc:
            logger.debug(
                "draft_room_promote: releasing the connection used to "
                "compensate files row id=%s failed: %s",
                file_id,
                release_exc,
            )


def _delete_promotion_row(db_pool: SQLiteConnectionPool, promotion_id: int) -> bool:
    """Compensation: remove a just-created ``draft_promotions`` row when a
    later step (phase update or enqueue) in the same attempt failed. Never
    raises — see :func:`_delete_file_row`."""
    try:
        conn = db_pool.get_connection()
    except Exception as exc:
        logger.warning(
            "draft_room_promote: could not obtain a connection to compensate "
            "draft_promotions row id=%s — an orphan row may now exist: %s",
            promotion_id,
            exc,
        )
        return False
    try:
        conn.execute("DELETE FROM draft_promotions WHERE id = ?", (promotion_id,))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception as rollback_exc:
            logger.debug(
                "draft_room_promote: rollback failed while compensating "
                "draft_promotions row id=%s: %s",
                promotion_id,
                rollback_exc,
            )
        logger.warning(
            "draft_room_promote: failed to compensate draft_promotions row "
            "id=%s after a later promotion step failed — an orphan "
            "draft_promotions row may now exist",
            promotion_id,
        )
        return False
    finally:
        try:
            db_pool.release_connection(conn)
        except Exception as release_exc:
            logger.debug(
                "draft_room_promote: releasing the connection used to "
                "compensate draft_promotions row id=%s failed: %s",
                promotion_id,
                release_exc,
            )


def _discard_safe(path: Optional[Path]) -> bool:
    """Remove copied bytes on a failed promotion. Returns ``True`` on success
    (including "already gone" and "nothing was ever reserved"), ``False`` if
    the removal itself raised — guarded so an ``OSError`` here (e.g. a
    permissions change mid-request) can never escape past the typed
    exception handling in :func:`_promote` and leave the caller without
    having run the rest of its compensation."""
    if path is None:
        return True
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning(
            "draft_room_promote: failed to remove copied bytes at %s — an "
            "orphan file may now exist on disk",
            path,
        )
        return False


async def _compensate(
    db_pool: SQLiteConnectionPool,
    *,
    promotion_id: Optional[int],
    file_id: Optional[int],
    dest_path: Optional[Path],
) -> bool:
    """Undo everything a failed promotion attempt created: the provenance
    row (if inserted), the ``files`` row (only ever one this attempt itself
    inserted), and the copied bytes.

    Never raises. Each step is independently guarded so a failure in one
    does not stop the others from running, and nothing here can replace or
    mask the exception the caller is already handling — that guard is
    layered on top of ``_delete_file_row``/``_delete_promotion_row``
    already never raising, as defense against something unexpected in the
    ``asyncio.to_thread`` marshalling itself. Returns whether every step
    succeeded; ``False`` means an orphan may genuinely still exist somewhere.
    """
    cleanup_ok = True
    if promotion_id is not None:
        try:
            if not await asyncio.to_thread(_delete_promotion_row, db_pool, promotion_id):
                cleanup_ok = False
        except Exception:
            cleanup_ok = False
            logger.warning(
                "draft_room_promote: compensation step _delete_promotion_row "
                "raised unexpectedly for id=%s",
                promotion_id,
            )
    if file_id is not None:
        try:
            if not await asyncio.to_thread(_delete_file_row, db_pool, file_id):
                cleanup_ok = False
        except Exception:
            cleanup_ok = False
            logger.warning(
                "draft_room_promote: compensation step _delete_file_row "
                "raised unexpectedly for id=%s",
                file_id,
            )
    try:
        if not await asyncio.to_thread(_discard_safe, dest_path):
            cleanup_ok = False
    except Exception:
        cleanup_ok = False
        logger.warning(
            "draft_room_promote: compensation step _discard_safe raised "
            "unexpectedly for %s",
            dest_path,
        )
    return cleanup_ok


async def _compensate_shielded(
    db_pool: SQLiteConnectionPool,
    *,
    promotion_id: Optional[int],
    file_id: Optional[int],
    dest_path: Optional[Path],
) -> bool:
    """Run :func:`_compensate` shielded from the enclosing cancel scope, with
    a bounded timeout, so a cancelled request cannot skip compensation
    wholesale.

    Starlette delivers a dropped client connection as anyio task-group
    cancellation (its ``BaseHTTPMiddleware`` layers run request handling in
    a child task group). anyio's cancellation is level-triggered: once a
    scope is cancelled, the *next* ``await`` inside it raises immediately,
    regardless of ordinary ``asyncio.shield`` — shield only protects a task
    from a direct ``task.cancel()``, not from the surrounding scope
    re-raising at its next checkpoint. ``anyio.CancelScope(shield=True)`` is
    the primitive that actually protects a block from an already-cancelled
    outer scope, which is why it is used here instead.
    """
    with anyio.move_on_after(_COMPENSATION_TIMEOUT_SECONDS, shield=True):
        return await _compensate(
            db_pool, promotion_id=promotion_id, file_id=file_id, dest_path=dest_path
        )
    logger.warning(
        "draft_room_promote: compensation timed out after %ss "
        "(promotion_id=%s file_id=%s) — an orphan may exist",
        _COMPENSATION_TIMEOUT_SECONDS,
        promotion_id,
        file_id,
    )
    return False


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

    dest_path: Optional[Path] = None
    file_id: Optional[int] = None
    promotion_id: Optional[int] = None
    try:
        dest_path = await _reserve_destination_path_shielded(upload_dir, file_name)
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
        # Cancellation/interrupt must unwind as itself, never be reported as
        # a promotion failure — but compensation still runs first, shielded
        # so it cannot itself be cut short by the same cancellation.
        is_control_flow = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))

        cleanup_ok = await _compensate_shielded(
            db_pool, promotion_id=promotion_id, file_id=file_id, dest_path=dest_path
        )

        if is_control_flow:
            raise

        # Recognized domain errors (duplicate / too-large / path-conflict)
        # carry a specific meaning the route maps to a specific status code
        # and must pass through unchanged (with `cleanup_incomplete`
        # updated). Anything else — a bare `sqlite3.IntegrityError` from a
        # concurrently deleted draft, an `OSError`, etc. — is wrapped into a
        # generic `DraftPromotionError` so the route's catch-all handler
        # returns the same clean error envelope every other failure here
        # does, instead of leaking a raw, unhandled exception type.
        if isinstance(exc, DraftPromotionError):
            exc.cleanup_incomplete = not cleanup_ok
            raise
        raise DraftPromotionError(
            f"promotion failed: {exc}", cleanup_incomplete=not cleanup_ok
        ) from exc

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
