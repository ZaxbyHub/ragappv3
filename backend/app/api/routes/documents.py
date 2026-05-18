"""
Documents API routes for file management and processing.

Provides endpoints for listing documents, uploading files, scanning directories,
and managing document processing status.
"""

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, List, Optional

import aiofiles
from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    get_background_processor,
    get_current_active_user,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_evaluate_policy,
    get_secret_manager,
    get_settings,
    get_user_accessible_vault_ids,
    get_vector_store,
    require_admin_role,
    require_vault_permission,
)
from app.config import Settings, settings
from app.limiter import limiter
from app.models.database import SQLiteConnectionPool
from app.security import csrf_protect
from app.services.background_tasks import BackgroundProcessor
from app.services.document_processor import (
    DocumentProcessingError,
    DocumentProcessor,
    DuplicateFileError,
)
from app.services.embeddings import EmbeddingService
from app.services.secret_manager import SecretManager
from app.services.upload_path import UploadPathProvider
from app.services.vector_store import VectorStore

# Magic byte signatures for file types where extension spoofing is high-risk.
# Text-based formats (txt, md, csv, json, yaml, etc.) have no fixed binary header
# and are intentionally excluded from this check.
_MAGIC_BYTES: dict[str, bytes] = {
    ".pdf": b"%PDF",
    ".docx": b"PK\x03\x04",
    ".xlsx": b"PK\x03\x04",
    ".xls": b"\xd0\xcf\x11\xe0",  # OLE Compound File
}


def _check_magic_bytes(extension: str, header: bytes) -> bool:
    """Return True if header matches expected magic bytes for the extension."""
    magic = _MAGIC_BYTES.get(extension)
    if magic is None:
        return True
    return header[: len(magic)] == magic


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


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/documents", tags=["documents"])


async def _optional_current_user(
    authorization: str | None = Header(None),
    db: sqlite3.Connection = Depends(get_db),
) -> dict | None:
    """Try to get JWT user, return None if auth is disabled or token invalid."""
    if not settings.users_enabled or not authorization:
        return None
    try:
        return await get_current_active_user(authorization=authorization, db=db)
    except HTTPException:
        return None


def _record_document_action(
    file_id: int,
    action: str,
    status: str,
    user_id: str,
    secret_manager: SecretManager,
    conn: sqlite3.Connection,
) -> None:
    key, key_version = secret_manager.get_hmac_key()
    message = f"{file_id}|{action}|{status}|{user_id}"
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    conn.execute(
        """
        INSERT INTO document_actions(file_id, action, status, user_id, hmac_sha256)
        VALUES (?, ?, ?, ?, ?)
        """,
        (file_id, action, status, user_id, digest),
    )


@router.post("/admin/retry/{file_id}")
@limiter.limit(settings.admin_rate_limit)
async def retry_document(
    file_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_admin_role),
    csrf_token: str = Depends(csrf_protect),
    secret_manager: SecretManager = Depends(get_secret_manager),
    background_processor: BackgroundProcessor = Depends(get_background_processor),
    current_user: dict | None = Depends(_optional_current_user),
) -> dict:
    try:
        cursor = await asyncio.to_thread(
            conn.execute, "SELECT file_path, vault_id FROM files WHERE id = ?", (file_id,)
        )
        row = await asyncio.to_thread(cursor.fetchone)
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")

        # Ensure processor is running
        if not background_processor.is_running:
            await background_processor.start()

        await background_processor.enqueue(row["file_path"], vault_id=row["vault_id"], file_id=file_id)

        user_id = (
            str(current_user["id"])
            if current_user and current_user.get("id")
            else user.get("id", "unknown")
        )
        await asyncio.to_thread(
            _record_document_action,
            file_id,
            "retry",
            "scheduled",
            user_id,
            secret_manager,
            conn,
        )
        await asyncio.to_thread(conn.commit)
        return {"file_id": file_id, "status": "scheduled"}
    except HTTPException:
        raise
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        logger.exception("Error reprocessing document %d", file_id)
        user_id = (
            str(current_user["id"])
            if current_user and current_user.get("id")
            else user.get("id", "unknown")
        )
        await asyncio.to_thread(
            _record_document_action,
            file_id,
            "retry",
            "error",
            user_id,
            secret_manager,
            conn,
        )
        await asyncio.to_thread(conn.commit)
        raise HTTPException(status_code=500, detail=f"Retry failed: {exc}")


class DocumentResponse(BaseModel):
    """Response model for a document record - frontend compatible."""

    id: int
    file_name: str
    filename: str  # Frontend alias
    file_path: str
    status: str
    chunk_count: int
    size: Optional[int] = None  # Frontend expects size
    created_at: Optional[str]
    processed_at: Optional[str]
    error_message: Optional[str] = None
    phase: Optional[str] = None
    phase_message: Optional[str] = None
    progress_percent: Optional[float] = None
    processed_units: Optional[int] = None
    total_units: Optional[int] = None
    unit_label: Optional[str] = None
    phase_started_at: Optional[str] = None
    processing_started_at: Optional[str] = None
    enrichment_status: Optional[str] = None
    enrichment_error: Optional[str] = None
    metadata: Optional[dict] = None  # Frontend expects metadata

    model_config = ConfigDict(from_attributes=True)


class DocumentListResponse(BaseModel):
    """Response model for listing documents - frontend compatible with total."""

    documents: List[DocumentResponse]
    total: int


class DocumentStatsResponse(BaseModel):
    """Response model for document statistics - frontend compatible."""

    total_documents: int  # Frontend expects this field
    total_chunks: int
    total_size_bytes: int = 0  # Frontend expects this field
    documents_by_status: dict = Field(
        default_factory=dict
    )  # Frontend expects this field
    total_files: int = 0  # Backward compatibility alias
    status: str = "success"


class UploadResponse(BaseModel):
    """Response model for file upload - frontend compatible."""

    file_id: int
    file_name: str
    id: int  # Frontend alias for file_id
    filename: str  # Frontend alias for file_name
    status: str
    message: str


class ScanResponse(BaseModel):
    """Response model for directory scan - frontend compatible."""

    files_enqueued: int
    status: str
    message: str
    added: int  # Frontend alias for files_enqueued
    scanned: int  # Frontend expects this field (total files scanned)
    errors: List[str] = Field(default_factory=list)  # Frontend expects this field


class DeleteResponse(BaseModel):
    """Response model for document deletion."""

    file_id: int
    status: str
    message: str


class BatchDeleteResponse(BaseModel):
    """Response model for batch document deletion."""

    deleted_count: int
    failed_ids: List[str]


class DeleteAllVaultResponse(BaseModel):
    """Response model for deleting all documents in a vault."""

    deleted_count: int
    vault_id: int


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_document_roots(settings_dep: Settings, vault_id: int) -> list[Path]:
    return [
        settings_dep.vault_uploads_dir(vault_id),
        settings_dep.vault_documents_dir(vault_id),
        settings_dep.uploads_dir,
        settings_dep.documents_dir,
        settings_dep.library_dir,
    ]


def _row_to_document_response(row: sqlite3.Row) -> DocumentResponse:
    """Convert a database row to a DocumentResponse."""
    keys = row.keys()
    file_name = row["file_name"]
    chunk_count = row["chunk_count"] or 0
    status = row["status"]
    error_message = row["error_message"] if "error_message" in keys else None
    phase = row["phase"] if "phase" in keys else None
    phase_message = row["phase_message"] if "phase_message" in keys else None
    progress_percent = row["progress_percent"] if "progress_percent" in keys else None
    processed_units = row["processed_units"] if "processed_units" in keys else None
    total_units = row["total_units"] if "total_units" in keys else None
    unit_label = row["unit_label"] if "unit_label" in keys else None
    phase_started_at = row["phase_started_at"] if "phase_started_at" in keys else None
    processing_started_at = (
        row["processing_started_at"] if "processing_started_at" in keys else None
    )
    enrichment_status = row["enrichment_status"] if "enrichment_status" in keys else None
    enrichment_error = row["enrichment_error"] if "enrichment_error" in keys else None
    return DocumentResponse(
        id=row["id"],
        file_name=file_name,
        filename=file_name,  # Frontend alias
        file_path=row["file_path"],
        status=status,
        chunk_count=chunk_count,
        size=row["file_size"]
        if "file_size" in row.keys() and row["file_size"] is not None
        else None,
        created_at=row["created_at"],
        processed_at=row["processed_at"],
        error_message=error_message,
        phase=phase,
        phase_message=phase_message,
        progress_percent=progress_percent,
        processed_units=processed_units,
        total_units=total_units,
        unit_label=unit_label,
        phase_started_at=phase_started_at,
        processing_started_at=processing_started_at,
        enrichment_status=enrichment_status,
        enrichment_error=enrichment_error,
        metadata={
            "status": status,
            "chunk_count": chunk_count,
            "chunks": chunk_count,  # Backward compatibility
            # Keep progress fields mirrored for legacy metadata-based clients.
            "error_message": error_message,
            "phase": phase,
            "phase_message": phase_message,
            "progress_percent": progress_percent,
            "processed_units": processed_units,
            "total_units": total_units,
            "unit_label": unit_label,
            "phase_started_at": phase_started_at,
            "processing_started_at": processing_started_at,
            "enrichment_status": enrichment_status,
            "enrichment_error": enrichment_error,
        },
    )


def _build_files_fts_query(raw_search: str) -> str:
    # Strip hyphens: FTS5 treats hyphens as column-filter prefix (col:term).
    # Including hyphens in tokens causes OperationalError on hyphenated searches.
    # FTS5's default tokenizer already splits on hyphens during indexing,
    # so individual tokens (my, doc, pdf) still match hyphenated filenames.
    normalized = raw_search.lower().replace("-", " ")
    tokens = re.findall(r"[A-Za-z0-9_]+", normalized)
    return " ".join(f"{token}*" for token in tokens[:8])


@router.get("", response_model=DocumentListResponse)
@router.get("/", response_model=DocumentListResponse)
async def list_documents(
    vault_id: Optional[int] = Query(None, description="Filter by vault ID"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(50, ge=1, le=200, description="Items per page"),
    search: Optional[str] = Query(
        None,
        description="Filter by document name or metadata fields (case-insensitive substring)",
    ),
    status: Optional[str] = Query(None, description="Filter by processing status"),
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    List documents from the files table with pagination.

    Returns a list of files with their id, file_name, file_path, status,
    chunk_count, created_at, and processed_at fields.
    Optionally filter by vault_id, search (document name/metadata substring), or status.
    Supports pagination via page/per_page.
    """
    offset = (page - 1) * per_page

    # Build search/status filter clause additions
    extra_where: list[str] = []
    extra_params: list = []
    if search and search.strip():
        fts_query = _build_files_fts_query(search.strip())
        if fts_query:
            extra_where.append(
                """(
                    LOWER(file_name) LIKE ?
                    OR id IN (
                        SELECT rowid FROM files_search_fts
                        WHERE files_search_fts MATCH ?
                    )
                )"""
            )
            extra_params.append(f"%{search.strip().lower()}%")
            extra_params.append(fts_query)
        else:
            extra_where.append("LOWER(file_name) LIKE ?")
            extra_params.append(f"%{search.strip().lower()}%")
    if status and status.strip():
        extra_where.append("status = ?")
        extra_params.append(status.strip())

    def _extra_clause(prefix: str = "AND") -> str:
        if not extra_where:
            return ""
        return f" {prefix} " + " AND ".join(extra_where)

    # Check vault permissions
    if vault_id is not None:
        if not await evaluate(user, "vault", vault_id, "read"):
            raise HTTPException(status_code=403, detail="Access denied to vault")
        # Count total
        count_cursor = await asyncio.to_thread(
            conn.execute,
            f"SELECT COUNT(*) FROM files WHERE vault_id = ?{_extra_clause()}",
            (vault_id, *extra_params),
        )
        total = (await asyncio.to_thread(count_cursor.fetchone))[0]
        # Query with specific vault_id + pagination
        cursor = await asyncio.to_thread(
            conn.execute,
            f"""
            SELECT id, file_name, file_path, status, chunk_count, file_size,
                   created_at, processed_at, error_message, phase, phase_message,
                   progress_percent, processed_units, total_units, unit_label,
                   phase_started_at, processing_started_at, enrichment_status,
                   enrichment_error
            FROM files
            WHERE vault_id = ?{_extra_clause()}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (vault_id, *extra_params, per_page, offset),
        )
    else:
        # For non-admins without vault_id, get accessible vaults
        if user.get("role") not in ("admin", "superadmin"):
            accessible_vaults = get_user_accessible_vault_ids(user, conn)
            if not accessible_vaults:
                return DocumentListResponse(documents=[], total=0)
            placeholders = ",".join("?" * len(accessible_vaults))
            # Count total
            count_cursor = await asyncio.to_thread(
                conn.execute,
                f"SELECT COUNT(*) FROM files WHERE vault_id IN ({placeholders}){_extra_clause()}",
                (*accessible_vaults, *extra_params),
            )
            total = (await asyncio.to_thread(count_cursor.fetchone))[0]
            # Query with vault_id IN clause + pagination
            cursor = await asyncio.to_thread(
                conn.execute,
                f"""
                SELECT id, file_name, file_path, status, chunk_count, file_size,
                       created_at, processed_at, error_message, phase, phase_message,
                       progress_percent, processed_units, total_units, unit_label,
                       phase_started_at, processing_started_at, enrichment_status,
                       enrichment_error
                FROM files
                WHERE vault_id IN ({placeholders}){_extra_clause()}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*accessible_vaults, *extra_params, per_page, offset),
            )
        else:
            # Admins can see all documents
            base_where = _extra_clause("WHERE")
            count_cursor = await asyncio.to_thread(
                conn.execute,
                f"SELECT COUNT(*) FROM files{base_where}",
                extra_params,
            )
            total = (await asyncio.to_thread(count_cursor.fetchone))[0]
            cursor = await asyncio.to_thread(
                conn.execute,
                f"""
                SELECT id, file_name, file_path, status, chunk_count, file_size,
                       created_at, processed_at, error_message, phase, phase_message,
                       progress_percent, processed_units, total_units, unit_label,
                       phase_started_at, processing_started_at, enrichment_status,
                       enrichment_error
                FROM files{base_where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*extra_params, per_page, offset),
            )
    rows = await asyncio.to_thread(cursor.fetchall)

    documents = [_row_to_document_response(row) for row in rows]

    return DocumentListResponse(documents=documents, total=total)


class DocumentStatusResponse(BaseModel):
    """Phase-aware status response used by the upload UI to poll indexing.

    `status` stays in the canonical 4-value enum
    ('pending','processing','indexed','error'); upload/queued/parsing/
    chunking/embedding/writing-index detail lives in `phase` and friends.
    `wiki_status` is derived from the latest `wiki_compile_jobs` row for
    this file, or 'pending' when `files.wiki_pending=1` and no job row
    has appeared yet, or null when no wiki job has been requested.
    """

    id: int
    filename: str
    status: str
    chunk_count: int
    error_message: Optional[str] = None
    processed_at: Optional[str] = None
    # Phase-aware progress
    phase: Optional[str] = None
    phase_message: Optional[str] = None
    progress_percent: Optional[float] = None
    processed_units: Optional[int] = None
    total_units: Optional[int] = None
    unit_label: Optional[str] = None
    phase_started_at: Optional[str] = None
    processing_started_at: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    # Wiki state (derived)
    wiki_status: Optional[str] = None
    wiki_phase: Optional[str] = None
    wiki_job_id: Optional[int] = None
    enrichment_status: Optional[str] = None
    enrichment_error: Optional[str] = None


@router.get("/{file_id}/status", response_model=DocumentStatusResponse)
async def get_document_status(
    file_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Return the phase-aware ingest status of a single document.

    Used by the composer attachment tray and the Documents upload queue to
    poll progress. Returns 404 when the file doesn't exist and 403 when
    the user lacks read access to the file's vault.
    """
    cursor = await asyncio.to_thread(
        conn.execute,
        """
        SELECT id, vault_id, file_name, status, chunk_count, error_message,
               processed_at, phase, phase_message, progress_percent,
               processed_units, total_units, unit_label, phase_started_at,
               processing_started_at, wiki_pending, enrichment_status,
               enrichment_error
        FROM files WHERE id = ?
        """,
        (file_id,),
    )
    row = await asyncio.to_thread(cursor.fetchone)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")

    if not await evaluate(user, "vault", row["vault_id"], "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")

    # Derive wiki state from the latest wiki_compile_jobs row for this file.
    # We look up by trigger_id="file:<id>" because that's how DocumentProcessor
    # tags ingest jobs (see services/document_processor.py).
    wiki_status: Optional[str] = None
    wiki_phase: Optional[str] = None
    wiki_job_id: Optional[int] = None
    try:
        wiki_cursor = await asyncio.to_thread(
            conn.execute,
            """
            SELECT id, status FROM wiki_compile_jobs
            WHERE trigger_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (f"file:{file_id}",),
        )
        wiki_row = await asyncio.to_thread(wiki_cursor.fetchone)
        if wiki_row is not None:
            wiki_job_id = int(wiki_row["id"])
            wiki_status = wiki_row["status"]
            wiki_phase = wiki_row["status"]
        elif _safe_get(row, "wiki_pending", 0):
            # Job hasn't appeared yet but processor has signalled intent.
            wiki_status = "pending"
            wiki_phase = "pending"
    except sqlite3.Error:
        # wiki_compile_jobs may not exist on very old test fixtures; treat as no wiki.
        pass

    # elapsed_seconds: from processing_started_at to now, when applicable.
    # We compute on the server so the frontend doesn't need clock-skew handling.
    elapsed_seconds: Optional[float] = None
    started = _safe_get(row, "processing_started_at")
    if started:
        try:
            from datetime import datetime, timezone
            # SQLite stores CURRENT_TIMESTAMP as 'YYYY-MM-DD HH:MM:SS' (UTC, no tz).
            started_dt = datetime.strptime(str(started), "%Y-%m-%d %H:%M:%S")
            started_dt = started_dt.replace(tzinfo=timezone.utc)
            elapsed_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - started_dt).total_seconds(),
            )
        except (ValueError, TypeError):
            elapsed_seconds = None

    return DocumentStatusResponse(
        id=row["id"],
        filename=row["file_name"],
        status=row["status"],
        chunk_count=row["chunk_count"] or 0,
        error_message=_safe_get(row, "error_message"),
        processed_at=_safe_get(row, "processed_at"),
        phase=_safe_get(row, "phase"),
        phase_message=_safe_get(row, "phase_message"),
        progress_percent=_safe_get(row, "progress_percent"),
        processed_units=_safe_get(row, "processed_units"),
        total_units=_safe_get(row, "total_units"),
        unit_label=_safe_get(row, "unit_label"),
        phase_started_at=_safe_get(row, "phase_started_at"),
        processing_started_at=_safe_get(row, "processing_started_at"),
        elapsed_seconds=elapsed_seconds,
        wiki_status=wiki_status,
        wiki_phase=wiki_phase,
        wiki_job_id=wiki_job_id,
        enrichment_status=_safe_get(row, "enrichment_status"),
        enrichment_error=_safe_get(row, "enrichment_error"),
    )


@router.get("/{file_id}/raw")
async def get_document_raw(
    file_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    settings_dep: Settings = Depends(get_settings),
):
    """Stream original document bytes for authenticated users with vault read access."""
    cursor = await asyncio.to_thread(
        conn.execute,
        """
        SELECT id, vault_id, file_name, file_path
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    )
    row = await asyncio.to_thread(cursor.fetchone)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")

    vault_id = row["vault_id"]
    if not await evaluate(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")

    try:
        file_path = Path(row["file_path"]).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Document file not found")

    allowed_roots = [
        root.resolve(strict=False) for root in _allowed_document_roots(settings_dep, vault_id)
    ]
    if not any(_path_is_within(file_path, root) for root in allowed_roots):
        logger.warning(
            "Refusing to serve document %s outside configured document roots: %s",
            file_id,
            file_path,
        )
        raise HTTPException(status_code=404, detail="Document file not found")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Document file not found")

    media_type = mimetypes.guess_type(row["file_name"])[0] or "application/octet-stream"
    is_pdf = media_type == "application/pdf" or row["file_name"].lower().endswith(".pdf")
    response = FileResponse(
        path=file_path,
        media_type=media_type,
        filename=row["file_name"],
        content_disposition_type="inline" if is_pdf else "attachment",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _safe_get(row, key: str, default=None):
    """sqlite3.Row.get-like helper: returns default when key is missing.

    sqlite3.Row supports indexing by name but raises IndexError on missing
    keys; in tests with hand-crafted dicts this avoids surprises.
    """
    try:
        if hasattr(row, "keys"):
            return row[key] if key in row.keys() else default
        return row[key]  # tuple/dict
    except (KeyError, IndexError, TypeError):
        return default


@router.get("/stats", response_model=DocumentStatsResponse)
async def get_document_stats(
    vault_id: Optional[int] = Query(None, description="Filter by vault ID"),
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Get counts of files and chunks.

    Returns total number of files in the database, total chunks,
    total size in bytes, and documents grouped by status.
    Optionally filter by vault_id.
    """
    # Determine vault filter for queries
    vault_filter_sql = ""
    vault_filter_params: tuple = ()

    # Check vault permissions
    if vault_id is not None:
        if not await evaluate(user, "vault", vault_id, "read"):
            raise HTTPException(status_code=403, detail="Access denied to vault")
        vault_filter_sql = "WHERE vault_id = ?"
        vault_filter_params = (vault_id,)
    else:
        # For non-admins without vault_id, get accessible vaults
        if user.get("role") not in ("admin", "superadmin"):
            accessible_vaults = get_user_accessible_vault_ids(user, conn)
            if not accessible_vaults:
                return DocumentStatsResponse(
                    total_documents=0,
                    total_chunks=0,
                    total_size_bytes=0,
                    documents_by_status={},
                    total_files=0,
                )
            placeholders = ",".join("?" * len(accessible_vaults))
            vault_filter_sql = f"WHERE vault_id IN ({placeholders})"
            vault_filter_params = tuple(accessible_vaults)

    # Get total files count
    cursor = await asyncio.to_thread(
        conn.execute,
        f"SELECT COUNT(*) as total_files FROM files {vault_filter_sql}".strip(),
        vault_filter_params,
    )
    row = await asyncio.to_thread(cursor.fetchone)
    total_files = row["total_files"]

    # Get total chunks count
    cursor = await asyncio.to_thread(
        conn.execute,
        f"SELECT COALESCE(SUM(chunk_count), 0) as total_chunks FROM files {vault_filter_sql}".strip(),
        vault_filter_params,
    )
    row = await asyncio.to_thread(cursor.fetchone)
    total_chunks = row["total_chunks"]

    # Get total size (sum of file_size if column exists, otherwise 0)
    try:
        cursor = await asyncio.to_thread(
            conn.execute,
            f"SELECT COALESCE(SUM(file_size), 0) as total_size FROM files {vault_filter_sql}".strip(),
            vault_filter_params,
        )
        row = await asyncio.to_thread(cursor.fetchone)
        total_size_bytes = row["total_size"] or 0
    except sqlite3.OperationalError:
        total_size_bytes = 0

    # Get documents grouped by status
    cursor = await asyncio.to_thread(
        conn.execute,
        f"SELECT status, COUNT(*) as count FROM files {vault_filter_sql} GROUP BY status".strip(),
        vault_filter_params,
    )
    rows = await asyncio.to_thread(cursor.fetchall)
    documents_by_status = {row["status"]: row["count"] for row in rows}

    return DocumentStatsResponse(
        total_documents=total_files,  # Frontend field
        total_chunks=total_chunks,
        total_size_bytes=total_size_bytes,
        documents_by_status=documents_by_status,
        total_files=total_files,  # Backward compatibility
    )


@router.post("", response_model=UploadResponse)
@router.post("/", response_model=UploadResponse)
async def upload_document_root(
    request: Request,
    file: Optional[UploadFile] = None,
    vault_id: int = Query(..., description="Target vault ID (required — no default)"),
    settings_dep: Settings = Depends(get_settings),
    vector_store: VectorStore = Depends(get_vector_store),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    db_pool: SQLiteConnectionPool = Depends(get_db_pool),
    background_processor: BackgroundProcessor = Depends(get_background_processor),
    user: dict = Depends(require_vault_permission("write")),
):
    """
    Upload endpoint at root /documents for frontend compatibility.
    Delegates to the main upload handler.

    Returns promptly after the file is durably saved and a `files` row exists
    with status='pending' / phase='queued'. Actual parsing/chunking/embedding
    runs in the background processor; clients poll
    ``GET /documents/{file_id}/status`` for progress.
    """
    return await _do_upload(
        request,
        file,
        settings_dep,
        vector_store,
        embedding_service,
        db_pool,
        background_processor,
        vault_id,
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_document(
    request: Request,
    file: Optional[UploadFile] = None,
    vault_id: int = Query(..., description="Target vault ID (required — no default)"),
    settings_dep: Settings = Depends(get_settings),
    vector_store: VectorStore = Depends(get_vector_store),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    db_pool: SQLiteConnectionPool = Depends(get_db_pool),
    background_processor: BackgroundProcessor = Depends(get_background_processor),
    user: dict = Depends(require_vault_permission("write")),
):
    """
    Upload a file with strict security controls and queue it for indexing.

    Validates filename, extension, and file size before saving. Saves the
    uploaded file to settings.uploads_dir using aiofiles, registers a `files`
    row with status='pending' / phase='queued', and enqueues the row for the
    background processor. Returns immediately so the client can poll
    ``GET /documents/{file_id}/status`` for phase-aware progress.
    """
    return await _do_upload(
        request,
        file,
        settings_dep,
        vector_store,
        embedding_service,
        db_pool,
        background_processor,
        vault_id,
    )


async def _do_upload(
    request: Request,
    file: Optional[UploadFile],
    settings_dep: Settings,
    vector_store: VectorStore,
    embedding_service: EmbeddingService,
    db_pool: SQLiteConnectionPool,
    background_processor: BackgroundProcessor,
    vault_id: int,
) -> UploadResponse:
    # Validate file is provided
    if file is None:
        raise HTTPException(status_code=400, detail="No file provided")

    # Validate filename is not empty
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename cannot be empty")

    # Ensure uploads directory exists
    provider = UploadPathProvider()
    upload_dir = provider.get_upload_dir(vault_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename
    file_name = secure_filename(file.filename or "unnamed_file")
    if not file_name:
        file_name = "unnamed_file.txt"

    # Ensure file has an extension for validation
    if not Path(file_name).suffix:
        file_name = f"{file_name}.txt"

    # Validate file extension
    file_suffix = Path(file_name).suffix.lower()
    if file_suffix not in settings_dep.allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"File extension '{file_suffix}' not allowed. Allowed: {settings_dep.allowed_extensions}",
        )

    # Validate file size from content-length header
    max_size_bytes = settings_dep.max_file_size_mb * 1024 * 1024
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_size_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max size: {settings.max_file_size_mb}MB",
                )
        except ValueError:
            pass  # Invalid content-length header, will check during streaming

    # Generate safe file path
    file_path = upload_dir / file_name

    # Handle duplicate file names atomically (avoid TOCTTOU race)
    counter = 0
    original_path = file_path
    while True:
        try:
            # O_EXCL ensures atomic create — fails if file exists
            fd = os.open(str(file_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            counter += 1
            file_path = upload_dir / f"{original_path.stem}_{counter}{original_path.suffix}"

    # Path safety: ensure file_path is within upload_dir
    # Clean up the atomically-created file if the safety check fails
    try:
        resolved_path = file_path.resolve()
        resolved_upload_dir = upload_dir.resolve()
        if not str(resolved_path).startswith(str(resolved_upload_dir)):
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Invalid file path")
    except HTTPException:
        raise
    except (OSError, ValueError):
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Invalid file path")

    temp_file_path = None
    try:
        # Read first 8 bytes for magic byte validation before streaming the rest.
        header_bytes = await file.read(8)
        if not header_bytes:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="File is empty")
        if not _check_magic_bytes(file_suffix, header_bytes):
            file_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"File content does not match the declared extension '{file_suffix}'",
            )

        # Save file using aiofiles with chunked reading and size validation
        total_bytes = len(header_bytes)
        temp_file_path = file_path
        async with aiofiles.open(temp_file_path, "wb") as f:
            await f.write(header_bytes)
            while chunk := await file.read(1024 * 1024):  # Read 1MB chunks
                total_bytes += len(chunk)
                if total_bytes > max_size_bytes:
                    # Close and delete partial file
                    await f.close()
                    if temp_file_path.exists():
                        temp_file_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Max size: {settings.max_file_size_mb}MB",
                    )
                await f.write(chunk)

        # Async ingestion: register the file row synchronously (so duplicate
        # detection still happens at the request, not in the worker), then
        # enqueue the existing row for background processing. The route
        # returns immediately with status='pending' / phase='queued' and the
        # client polls GET /documents/{file_id}/status for phase progress.
        from app.services.document_progress import (
            PHASE_QUEUED,
            set_phase,
        )
        from app.utils.file_utils import compute_file_hash

        processor = DocumentProcessor(
            chunk_size_chars=settings_dep.chunk_size_chars,
            chunk_overlap_chars=settings_dep.chunk_overlap_chars,
            vector_store=vector_store,
            embedding_service=embedding_service,
            pool=db_pool,
        )

        try:
            file_hash = compute_file_hash(str(file_path))

            # Phase 1: route-side duplicate check + row insert. Worker will
            # NOT re-run these because we pass file_id on the queue task.
            #
            # The check uses ``_check_duplicate_in_flight`` (matches pending /
            # processing / indexed) rather than the legacy ``_check_duplicate``
            # (indexed only) so two concurrent uploads of the same file
            # collapse to a single ingestion instead of both racing through
            # to the partial unique index ``idx_files_hash_vault_indexed``,
            # which would surface as a generic IntegrityError on the loser.
            conn = db_pool.get_connection()
            try:
                duplicate = processor._check_duplicate_in_flight(
                    file_hash, conn, vault_id
                )
                if duplicate:
                    # Don't leak the existing file's storage path in the 409
                    # detail (info disclosure). file_id + status + hash are
                    # enough for the client to reconcile against /documents.
                    file_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"File with hash {file_hash} already exists in "
                            f"this vault (status={duplicate['status']}, "
                            f"file_id={duplicate['id']}). Uploaded copy was "
                            f"cleaned up."
                        ),
                    )

                # _insert_or_get_file_record itself can raise DuplicateFileError
                # if the partial unique index trips (race window between the
                # in-flight check and the INSERT/UPDATE). Translate to 409.
                try:
                    file_id = processor._insert_or_get_file_record(
                        str(file_path),
                        file_hash,
                        conn,
                        vault_id,
                        "upload",
                        None,
                        None,
                    )
                except DuplicateFileError as e:
                    file_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=409,
                        detail=f"{e} (uploaded file was cleaned up)",
                    )
                conn.commit()
            finally:
                db_pool.release_connection(conn)

            # Mark queued phase; status stays 'pending' until the worker
            # transitions it to 'processing'. mark_processing_started=False
            # because actual processing has not started yet — only queueing.
            set_phase(
                db_pool,
                file_id,
                phase=PHASE_QUEUED,
                message="Queued for processing",
            )

            await background_processor.enqueue(
                file_path=str(file_path),
                source="upload",
                vault_id=vault_id,
                file_id=file_id,
            )

            return UploadResponse(
                file_id=file_id,
                file_name=file_name,
                id=file_id,
                filename=file_name,
                status="pending",
                message=(
                    f"File '{file_name}' uploaded and queued for processing. "
                    f"Poll GET /documents/{file_id}/status for progress."
                ),
            )
        except HTTPException:
            # Clean up partial file only if it still exists; duplicate path
            # already removed it. Don't unconditionally unlink — the file is
            # legitimately on disk for accepted uploads.
            raise
        except DocumentProcessingError as e:
            file_path.unlink(missing_ok=True)
            logger.exception("Document processing error for file: %s", file_name)
            raise HTTPException(status_code=500, detail=f"Processing error: {e}")
        except Exception as e:
            file_path.unlink(missing_ok=True)
            logger.exception("Unexpected error registering file: %s", file_name)
            raise HTTPException(status_code=500, detail=f"Server error: {e}")
    except HTTPException:
        # Validation / size-limit errors already cleaned up partial files inline.
        raise
    except Exception as e:
        logger.exception("Error uploading file: %s", file_name)
        if temp_file_path and temp_file_path.exists():
            temp_file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


@router.post("/scan", response_model=ScanResponse)
async def scan_directories(
    request: Request,
    background_processor: BackgroundProcessor = Depends(get_background_processor),
    db_pool: SQLiteConnectionPool = Depends(get_db_pool),
    user: dict = Depends(require_admin_role),
):
    """
    Trigger a scan of configured directories for new files.

    Calls FileWatcher.scan_once() to find and enqueue new files
    from uploads_dir and library_dir that are not in the database.

    Uses the singleton BackgroundProcessor that runs continuously in the background.
    """
    from app.services.file_watcher import FileWatcher

    # Ensure processor is running (it should be from lifespan, but double-check)
    if not background_processor.is_running:
        await background_processor.start()

    try:
        watcher = FileWatcher(background_processor, pool=db_pool)

        # Perform scan
        files_enqueued = await watcher.scan_once()

        if files_enqueued > 0:
            message = (
                f"Scan complete: {files_enqueued} new files enqueued for processing"
            )
        else:
            message = "Scan complete: no new files found"

        return ScanResponse(
            files_enqueued=files_enqueued,
            status="success",
            message=message,
            added=files_enqueued,  # Frontend alias
            scanned=files_enqueued,  # Frontend expects this (at least files_enqueued)
            errors=[],  # Frontend expects this field
        )
    except Exception as e:
        logger.exception("Error during directory scan")
        raise HTTPException(status_code=500, detail=f"Scan failed: {e}")
    # Note: No finally block to stop processor - it runs continuously


async def _delete_file_record(
    conn: sqlite3.Connection,
    vector_store: VectorStore,
    file_id: int,
    file_name: str,
    vault_id: int,
) -> None:
    """Delete one file and its derived data with consistent cleanup."""
    try:
        try:
            db = vector_store.db
            if db is not None and "chunks" in await db.table_names():
                vector_store.table = await db.open_table("chunks")
                deleted_chunks = await vector_store.delete_by_file(str(file_id))
                logger.info(
                    "Deleted %d chunks from vector store for file_id %s",
                    deleted_chunks,
                    file_id,
                )
            else:
                logger.debug(
                    "Chunks table not found, skipping vector store deletion for file_id %s",
                    file_id,
                )
        except Exception as e:
            logger.warning("Error deleting chunks from vector store: %s", e)

        try:
            from app.services.wiki_store import WikiStore as _WikiStore

            await asyncio.to_thread(
                lambda: _WikiStore(conn).mark_claims_stale_by_file(file_id, vault_id)
            )
        except Exception as e:
            logger.warning("mark_claims_stale_by_file(%d) failed: %s", file_id, e)

        await asyncio.to_thread(
            conn.execute, "DELETE FROM files WHERE id = ?", (file_id,)
        )
        await asyncio.to_thread(conn.commit)
        logger.info("Deleted document '%s' (id: %d)", file_name, file_id)
    except Exception:
        await asyncio.to_thread(conn.rollback)
        raise


@router.delete("/{file_id}", response_model=DeleteResponse)
async def delete_document(
    file_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    vector_store: VectorStore = Depends(get_vector_store),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Delete a document by ID.

    Deletes the file record from the database and removes all associated
    chunks from the vector store. Returns 404 if the file is not found.
    """
    # Check if file exists and get vault_id for permission check
    cursor = await asyncio.to_thread(
        conn.execute,
        "SELECT id, file_name, vault_id FROM files WHERE id = ?",
        (file_id,),
    )
    row = await asyncio.to_thread(cursor.fetchone)

    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Document with id {file_id} not found"
        )

    file_name = row["file_name"]
    file_vault_id = row["vault_id"]

    # Check vault admin permission
    if not await evaluate(user, "vault", file_vault_id, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient vault permissions")

    try:
        await _delete_file_record(
            conn,
            vector_store,
            file_id,
            file_name,
            file_vault_id,
        )

        return DeleteResponse(
            file_id=file_id,
            status="success",
            message=f"Document '{file_name}' (id: {file_id}) deleted successfully",
        )
    except HTTPException:
        raise
    except Exception as e:
        await asyncio.to_thread(conn.rollback)
        logger.exception("Error deleting document %d", file_id)
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


@router.post("/batch", response_model=BatchDeleteResponse)
async def batch_delete_documents(
    request: Request,
    file_ids: List[str] = Body(
        ..., embed=True, description="List of file IDs to delete"
    ),
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    vector_store: VectorStore = Depends(get_vector_store),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Batch delete documents by IDs.

    Deletes multiple documents from the database and removes all associated
    chunks from the vector store. Returns count of successfully deleted
    documents and any failed IDs.
    """
    deleted_count = 0
    failed_ids: List[str] = []

    for file_id in file_ids:
        try:
            try:
                normalized_file_id = int(file_id)
            except (TypeError, ValueError):
                failed_ids.append(file_id)
                continue

            # Check if file exists
            cursor = await asyncio.to_thread(
                conn.execute,
                "SELECT id, file_name, vault_id FROM files WHERE id = ?",
                (normalized_file_id,),
            )
            row = await asyncio.to_thread(cursor.fetchone)

            if row is None:
                failed_ids.append(file_id)
                continue

            file_name = row["file_name"]
            file_vault_id = row["vault_id"]

            if not await evaluate(user, "vault", file_vault_id, "admin"):
                failed_ids.append(file_id)
                continue

            await _delete_file_record(
                conn,
                vector_store,
                normalized_file_id,
                file_name,
                file_vault_id,
            )
            deleted_count += 1

        except Exception:
            logger.exception("Error deleting document %s", file_id)
            failed_ids.append(file_id)

    return BatchDeleteResponse(
        deleted_count=deleted_count,
        failed_ids=failed_ids,
    )


@router.delete("/vault/{vault_id}/all", response_model=DeleteAllVaultResponse)
async def delete_all_vault_documents(
    vault_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_vault_permission("admin")),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Delete all documents in a vault.

    Deletes all file records from the database and removes all associated
    chunks from the vector store for the specified vault.
    """
    # Get all file IDs in the vault
    cursor = await asyncio.to_thread(
        conn.execute, "SELECT id, file_name FROM files WHERE vault_id = ?", (vault_id,)
    )
    rows = await asyncio.to_thread(cursor.fetchall)

    deleted_count = 0

    for row in rows:
        file_id = row["id"]
        file_name = row["file_name"]
        try:
            await _delete_file_record(
                conn,
                vector_store,
                file_id,
                file_name,
                vault_id,
            )
            deleted_count += 1

        except Exception:
            logger.exception(
                "Error deleting document %d from vault %d", file_id, vault_id
            )

    logger.info("Deleted %d documents from vault %d", deleted_count, vault_id)

    return DeleteAllVaultResponse(
        deleted_count=deleted_count,
        vault_id=vault_id,
    )


# Exception handler for validation errors (e.g., empty filename)
# This is registered at the app level in main.py
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Convert validation errors to 400 for empty filename cases only."""
    errors = exc.errors()
    for error in errors:
        if (
            error.get("loc") == ("body", "file")
            and "filename" in str(error.get("input", "")).lower()
        ):
            raise HTTPException(status_code=400, detail="Filename cannot be empty")
    # For all other validation errors, return standard 422
    # Convert errors to dict format for JSON serialization
    from fastapi.responses import JSONResponse

    error_dicts = [
        {
            "loc": error.get("loc"),
            "msg": error.get("msg"),
            "type": error.get("type"),
            "input": error.get("input"),
        }
        for error in errors
    ]
    return JSONResponse(status_code=422, content={"detail": error_dicts})
