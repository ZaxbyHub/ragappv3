"""
Durable artifact storage for the multimodal RAG foundation (issue #460).

This module coordinates three concerns that the issue keeps deliberately
separate from the parser-neutral atom model (:mod:`app.services.document_artifacts`):

1. **A confined per-vault asset filesystem**: extracted binary assets live under
   the configured vault artifact root and are stored only under *server-derived*
   opaque relative paths — never caller-supplied paths. Every read/delete
   re-resolves the relative path against the configured root and rejects
   traversal, symlink/reparse-escape, and wrong-vault roots.
2. **Generation-scoped persistence**: atoms/assets/stage rows for one source
   generation are published atomically (same transaction), old-generation rows
   are retired only after the new generation is durable, and assets are
   touched only via durable tombstones so a crash after the DB commit cannot
   leak bytes permanently.
3. **A sweep/reconciler**: ``artifact_delete_pending`` tombstones written in the
   same transaction that removes owning rows are retried by the background sweep
   and startup reconciler; a tombstone is removed only after confined deletion
   succeeds (or the path is confirmed absent).

Assets are content-addressed: ``asset_id`` is the SHA-256 digest of the bytes.
This makes reprocessing an unchanged generation idempotent and naturally
deduplicates identical extracted assets.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from app.config import settings
from app.services.document_artifacts import (
    DocumentAsset,
    DocumentAtom,
    validate_generation_atoms,
)

logger = logging.getLogger(__name__)

# A tombstone is retried a bounded number of times before being reported to the
# operator; it is never silently dropped while bytes may remain.
_MAX_UNLINK_ATTEMPTS = 5

# Safe raster MIME allowlist for asset serving / VLM transmission (issue #462).
# SVG/HTML and all other types are excluded. This is the single source of truth
# for "is this asset safe to present/send".
RASTER_MIME_ALLOWLIST = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
)


def sniff_raster_mime(data: bytes) -> Optional[str]:
    """Derive a safe raster MIME from the byte header (never the path).

    The asset path is extensionless content hashes, and the persisted
    ``document_assets.mime_type`` may drift, so the authoritative + independent
    signal is a lazy Pillow header decode. Returns a MIME in
    :data:`RASTER_MIME_ALLOWLIST` or ``None`` for unsupported/corrupt data.
    'MPO' (multi-picture JPEG) is a JPEG container and maps to ``image/jpeg``.
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:  # pragma: no cover — Pillow optional
        return None
    import io

    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
    except Exception:  # noqa: BLE001 — corrupt/undecodable header
        return None
    table = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "MPO": "image/jpeg",
        "GIF": "image/gif",
        "WEBP": "image/webp",
        "BMP": "image/bmp",
        "TIFF": "image/tiff",
    }
    return table.get(fmt)


# ---------------------------------------------------------------------------
# Path security
# ---------------------------------------------------------------------------


def artifact_root(vault_id: int, settings_obj=settings) -> Path:
    """The configured, per-vault asset root."""
    return settings_obj.vault_artifacts_dir(vault_id)


def resolve_confined(rel_path: str, vault_id: int) -> Optional[Path]:
    """Resolve a persisted relative asset path against the vault artifact root.

    Returns the resolved absolute :class:`Path` if it stays inside the root, or
    ``None`` (after logging) if it would escape. Attackers are not trusted to
    influence ``rel_path`` (it is server-derived), but defense-in-depth rejects
    absolute paths and path-traversal components anyway, and ``resolve()``
    collapses ``..``/symlinks before the containment check.

    Args:
        rel_path: A validated relative storage path, e.g. ``"<gen_short>/<sha>"``.
        vault_id: The owning vault (assets never cross vault roots).
    """
    p = Path(rel_path)
    if p.is_absolute():
        logger.warning("Refusing absolute asset rel_path: %s", rel_path)
        return None
    if ".." in p.parts:
        logger.warning("Refusing traversal asset rel_path: %s", rel_path)
        return None
    try:
        root = artifact_root(vault_id=vault_id).resolve()
        resolved = (root / p).resolve(strict=False)
    except (OSError, RuntimeError, ValueError, TypeError):
        logger.warning("Could not resolve asset rel_path %r", rel_path)
        return None
    if not _is_within(resolved, root):
        logger.warning(
            "Refusing asset rel_path outside vault root: %s", resolved
        )
        return None
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Asset filesystem
# ---------------------------------------------------------------------------


def _asset_rel_path(file_id: int, generation_hash: str, asset_id: str) -> str:
    """Server-derived relative path for an asset.

    Only contains the opaque file id, the opaque generation hash short and the
    content-addressable asset id — never any caller-controlled filename, which
    is what makes traversal/symlink attacks structurally impossible. Including
    ``file_id`` makes paths per-owner so two files that share identical content
    (and therefore the same ``generation_hash`` + ``asset_id``) never alias one
    another on disk (issue #460, final-critic finding 5).
    """
    gen_short = generation_hash[:12] or "gen"
    return f"{file_id}/{gen_short}/{asset_id}"


def compute_asset_rel_path(file_id: int, generation_hash: str, asset_id: str) -> str:
    """Public wrapper so planners and the materializer share one path formula.

    Used by the image path to build a :class:`DocumentAsset` value object before
    the bytes are materialized at publish time, guaranteeing the planned path
    exactly matches what ``store_asset_bytes`` will write.
    """
    return _asset_rel_path(file_id, generation_hash, asset_id)


def store_asset_bytes(
    *,
    file_id: int,
    vault_id: int,
    generation_hash: str,
    data: bytes,
    mime_type: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    extra_metadata: Optional[dict] = None,
) -> DocumentAsset:
    """Atomically place ``data`` under the vault artifact root and fingerprint it.

    ``asset_id`` is the SHA-256 digest of the bytes (content-addressed), which
    makes unchanged-generation reprocessing idempotent. The write is
    atomic (temp file + ``os.replace``) so a reader never sees a partial asset.

    Args:
        file_id: Owning file row.
        vault_id: Owning vault (root selection).
        generation_hash: Source generation this asset belongs to.
        data: Raw asset bytes.
        mime_type / width / height / extra_metadata: bounded metadata.

    Returns:
        A :class:`DocumentAsset` value object with the opaque id, sha256, and
        validated relative path.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    asset_id = sha256
    rel_path = _asset_rel_path(file_id, generation_hash, asset_id)
    root = artifact_root(vault_id=vault_id)
    dest = root / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(dest, data)
    return DocumentAsset(
        asset_id=asset_id,
        file_id=file_id,
        generation_hash=generation_hash,
        sha256=sha256,
        rel_path=rel_path,
        mime_type=mime_type,
        width=width,
        height=height,
        byte_size=len(data),
        metadata=extra_metadata or {},
    )


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-asset-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(dest))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def unlink_asset_rel(rel_path: str, vault_id: int) -> bool:
    """Confinedly delete one asset file, returning True when gone.

    Returns True if the path was removed or was already absent; False if it
    could not be resolved/removed (so the caller can keep/requeue the tombstone).
    Never deletes a path outside the configured vault artifact root.
    """
    path = resolve_confined(rel_path, vault_id)
    if path is None:
        logger.warning("Skipping unresolvable/out-of-root asset unlink: %s", rel_path)
        return False
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError as exc:
        logger.warning("Failed to unlink asset %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Durable tombstones
# ---------------------------------------------------------------------------


def enqueue_asset_cleanup(
    conn,
    *,
    file_id: int,
    vault_id: int,
    rel_paths: Iterable[str],
    generation_hash: Optional[str] = None,
) -> None:
    """Insert durable cleanup tombstones on the caller's connection (no commit).

    Called in the same transaction that removes the owning rows so a crash after
    the commit cannot leak bytes permanently. The background sweep retries
    confined deletion and removes the tombstone only after success.
    """
    for rel_path in rel_paths:
        try:
            conn.execute(
                "INSERT INTO artifact_delete_pending "
                "(file_id, vault_id, rel_path, generation_hash) "
                "VALUES (?, ?, ?, ?)",
                (file_id, vault_id, rel_path, generation_hash),
            )
        except Exception as exc:  # noqa: BLE001
            # A tombstone write must not abort the owning transaction. There is
            # no automatic rescan fallback: an unrecorded tombstone leaks its
            # bytes until operator intervention, so log it loudly.
            logger.error(
                "Could not enqueue asset cleanup for %s (file_id=%s); bytes may "
                "leak until operator reconciliation: %s",
                rel_path,
                file_id,
                exc,
            )


def collect_asset_rel_paths_for_file(conn, file_id: int) -> list[str]:
    """Return the exact, confined asset rel paths owned by ``file_id``."""
    try:
        cursor = conn.execute(
            "SELECT rel_path FROM document_assets WHERE file_id = ?", (file_id,)
        )
        return [row["rel_path"] for row in cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not collect asset paths for file_id=%s: %s", file_id, exc)
        return []


def devault_asset_rel_paths(conn, vault_id: int) -> list[str]:
    """Return every asset rel path for a vault (for vault deletion)."""
    cursor = conn.execute(
        "SELECT rel_path FROM document_assets WHERE file_id IN "
        "(SELECT id FROM files WHERE vault_id = ?)",
        (vault_id,),
    )
    return [row["rel_path"] for row in cursor.fetchall()]


def sweep_pending_asset_deletes(conn) -> tuple[int, int]:
    """Retry confined deletion of every pending asset tombstone.

    Returns ``(removed, remaining)``. A path is removed from the pending table
    only after confined unlink succeeds or the path is confirmed absent; paths
    that still fail after :data:`_MAX_UNLINK_ATTEMPTS` are left for a later
    sweep and logged, never silently dropped.
    """
    rows = conn.execute(
        "SELECT id, file_id, vault_id, rel_path, attempts "
        "FROM artifact_delete_pending"
    ).fetchall()
    removed = 0
    remaining = 0
    for row in rows:
        ok = unlink_asset_rel(row["rel_path"], row["vault_id"])
        if ok:
            conn.execute("DELETE FROM artifact_delete_pending WHERE id = ?", (row["id"],))
            removed += 1
        else:
            prev_attempts = row["attempts"] or 0
            new_attempts = prev_attempts + 1
            # Persist capped attempts so the row is not rewritten unboundedly
            # once it is clearly stuck, and only log the error the first time
            # the threshold is crossed (prevents per-sweep log spam while still
            # leaving the path for a later sweep / operator reconciliation).
            persist = min(new_attempts, _MAX_UNLINK_ATTEMPTS + 1)
            conn.execute(
                "UPDATE artifact_delete_pending SET attempts = ? WHERE id = ?",
                (persist, row["id"]),
            )
            if prev_attempts < _MAX_UNLINK_ATTEMPTS and new_attempts >= _MAX_UNLINK_ATTEMPTS:
                logger.error(
                    "Asset cleanup failed repeatedly (file_id=%s rel=%s attempts=%s); "
                    "requires operator reconciliation",
                    row["file_id"],
                    row["rel_path"],
                    new_attempts,
                )
            remaining += 1
    conn.commit()
    return removed, remaining


# ---------------------------------------------------------------------------
# Generation publication (DB side)
# ---------------------------------------------------------------------------


def publish_generation(
    conn,
    *,
    file_id: int,
    vault_id: int,
    generation_hash: str,
    atoms: Iterable[DocumentAtom],
    assets: Iterable[DocumentAsset],
    stage_states: Iterable[dict],
    parser_fingerprint: str,
    implementation_version: str,
) -> None:
    """Publish one source generation's rows atomically on ``conn`` (no commit).

    Inserts new-generation atom/asset/stage rows and retires *old* generations
    (any row whose ``generation_hash`` differs) in the same transaction. Retired
    asset paths are tombstoned first so the filesystem bytes are collected by
    the sweep after the new generation is durable. Reprocessing an unchanged
    generation is idempotent (content-addressed asset ids + ``INSERT OR REPLACE``
    on the unique keys).

    Args:
        conn: An open pooled connection (foreign_keys ON).
        file_id: Owning file row.
        vault_id: Owning vault.
        generation_hash: The generation being made authoritative.
        atoms: Ordered atoms of this generation.
        assets: Extracted binary assets of this generation.
        stage_states: List of stage-state dicts (see ``_upsert_stage``).
        parser_fingerprint / implementation_version: provenance for stage rows.
    """
    atoms = list(atoms)
    validation_error = validate_generation_atoms(atoms)
    if validation_error is not None:
        raise ValueError(f"refusing to publish invalid generation: {validation_error}")

    # Enforce the configured per-generation asset bounds so the declared config
    # knobs are real (issue #460, final-critic finding 6). Checked before any
    # tombstone/delete so a bound violation cannot partially retire the prior
    # generation.
    assets = list(assets)
    if len(assets) > settings.max_assets_per_generation:
        raise ValueError(
            f"refusing to publish generation with {len(assets)} assets "
            f"(> {settings.max_assets_per_generation})"
        )
    if sum(a.byte_size or 0 for a in assets) > settings.max_asset_bytes_per_generation:
        raise ValueError(
            f"refusing to publish generation with asset bytes "
            f"exceeding {settings.max_asset_bytes_per_generation}"
        )

    # Retire old generations (different hash): tombstone their assets, then
    # delete their atom/asset/stage rows. Never touches the current generation.
    old_paths = [
        row["rel_path"]
        for row in conn.execute(
            "SELECT rel_path FROM document_assets "
            "WHERE file_id = ? AND generation_hash <> ?",
            (file_id, generation_hash),
        ).fetchall()
    ]
    if old_paths:
        enqueue_asset_cleanup(
            conn, file_id=file_id, vault_id=vault_id, rel_paths=old_paths
        )
    conn.execute(
        "DELETE FROM document_assets WHERE file_id = ? AND generation_hash <> ?",
        (file_id, generation_hash),
    )
    conn.execute(
        "DELETE FROM document_atoms WHERE file_id = ? AND generation_hash <> ?",
        (file_id, generation_hash),
    )
    conn.execute(
        "DELETE FROM ingestion_stage_states WHERE file_id = ? AND generation_hash <> ?",
        (file_id, generation_hash),
    )

    for atom in atoms:
        conn.execute(
            "INSERT OR REPLACE INTO document_atoms "
            "(atom_id, schema_version, file_id, generation_hash, ordinal, kind, "
            " raw_text, page_number, bbox_json, bbox_coord_system, section_path_json, "
            " caption, parent_atom_id, asset_id, metadata_json, warnings_json, "
            " parser_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                atom.atom_id,
                atom.schema_version,
                atom.file_id,
                atom.generation_hash,
                atom.ordinal,
                atom.kind.value,
                atom.raw_text,
                atom.page_number,
                _json_or_none(atom.bbox),
                atom.bbox_coord_system,
                _json_or_none(list(atom.section_path)),
                atom.caption,
                atom.parent_atom_id,
                atom.asset_id,
                _json_or_none(atom.metadata),
                _json_or_none(atom.warnings),
                atom.parser_fingerprint,
            ),
        )

    for asset in assets:
        conn.execute(
            "INSERT OR REPLACE INTO document_assets "
            "(asset_id, file_id, generation_hash, sha256, rel_path, mime_type, "
            " width, height, byte_size, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                asset.asset_id,
                asset.file_id,
                asset.generation_hash,
                asset.sha256,
                asset.rel_path,
                asset.mime_type,
                asset.width,
                asset.height,
                asset.byte_size,
                _json_or_none(asset.metadata),
            ),
        )

    for stage in stage_states:
        _upsert_stage(
            conn,
            file_id=file_id,
            vault_id=vault_id,
            generation_hash=generation_hash,
            parser_fingerprint=parser_fingerprint,
            implementation_version=implementation_version,
            **stage,
        )

    conn.execute(
        "UPDATE files SET active_generation_hash = ? WHERE id = ?",
        (generation_hash, file_id),
    )


def _upsert_stage(
    conn,
    *,
    file_id: int,
    vault_id: int,
    generation_hash: str,
    parser_fingerprint: str,
    implementation_version: str,
    stage: str,
    status: str,
    atom_id_pk: Optional[int] = None,
    input_fingerprint: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    attempts: int = 0,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> None:
    """Upsert a single stage-state row.

    The partial unique indexes (file-scoped when ``atom_id_pk`` is None, and
    atom-scoped otherwise) provide the idempotency key. ``status`` must be one of
    the v1 vocabulary enforced by the schema CHECK.
    """
    # The partial unique index is on (file_id, generation_hash, atom_id, stage);
    # use INSERT OR REPLACE so re-publishing the same generation is idempotent.
    conn.execute(
        "INSERT OR REPLACE INTO ingestion_stage_states "
        "(file_id, atom_id, generation_hash, stage, status, input_fingerprint, "
        " implementation_version, parser_id, config_id, attempts, error_code, "
        " error_message, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            file_id,
            atom_id_pk,
            generation_hash,
            stage,
            status,
            input_fingerprint,
            implementation_version,
            parser_fingerprint or None,
            None,
            attempts,
            error_code,
            error_message,
            started_at,
            completed_at,
        ),
    )


def _json_or_none(value) -> Optional[str]:
    import json

    if value is None or value == {} or value == []:
        return None
    try:
        return json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
