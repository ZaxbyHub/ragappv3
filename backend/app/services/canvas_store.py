"""Canvas artifact storage service (issue #509).

Storage layer for the versioned code/document canvas. ``canvas_artifacts``
rows carry the stable public identity (``artifact_uid``) and the originating
chat lineage; ``canvas_versions`` rows are the append-only history — saves,
model range-edits, and restores all append, nothing is updated or deleted.

Follows the MemoryStore pool pattern (dedicated ``SQLiteConnectionPool``,
``get_connection``/``release_connection`` per call in ``try/finally``). Unlike
MemoryStore, the pool is resolved through ``get_pool`` at each call rather than
cached in ``__init__`` so the route module's lazy singleton survives the test
suite's per-test pool resets (``conftest._reset_db_pool`` closes and clears
``_pool_cache``; ``get_pool`` transparently recreates on the next call).

Concurrency contract for ``append_version``: the counter bump and the version
insert run inside one ``BEGIN IMMEDIATE`` transaction. The counter update is
guarded by ``WHERE current_version_no = ?``; a zero rowcount means a concurrent
append superseded the caller's ``base_version_no`` and the transaction rolls
back (the caller maps this to 409, or passes ``force=True`` to re-read the
current counter and append at current+1 so no version is ever lost).
"""

import hashlib
import json
import logging
import secrets
import sqlite3
from typing import Any, Dict, List, Optional

from app.config import settings
from app.models.database import SQLiteConnectionPool, get_pool

logger = logging.getLogger(__name__)

# Sentinel returned by append_version when the guarded counter update matched
# zero rows (stale base_version_no) and force=False. A unique object rather
# than None so "conflict" can never be confused with a legitimately absent row.
CANVAS_VERSION_CONFLICT: Any = object()

# Minted public ids: "cav_" prefix + 12 url-safe tokens (96 bits of entropy,
# unguessable and safe in URLs/headers without further encoding).
_ARTIFACT_UID_PREFIX = "cav_"
_ARTIFACT_UID_TOKEN_BYTES = 12


def mint_artifact_uid() -> str:
    """Mint a fresh public artifact id."""
    return _ARTIFACT_UID_PREFIX + secrets.token_urlsafe(_ARTIFACT_UID_TOKEN_BYTES)


def content_sha256(content: str) -> str:
    """SHA-256 hex digest of the UTF-8 bytes of ``content``."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _row_to_artifact(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "artifact_uid": row["artifact_uid"],
        "session_id": row["session_id"],
        "message_id": row["message_id"],
        "turn_id": row["turn_id"],
        "kind": row["kind"],
        "name": row["name"],
        "language": row["language"],
        "vault_id": row["vault_id"],
        "current_version_no": row["current_version_no"],
        "source_refs_json": row["source_refs_json"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_version(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "artifact_id": row["artifact_id"],
        "version_no": row["version_no"],
        "name": row["name"],
        "content": row["content"],
        "content_sha256": row["content_sha256"],
        "origin": row["origin"],
        "model_edit_json": row["model_edit_json"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


class CanvasStore:
    """SQLite-backed storage for canvas artifacts and their version history."""

    def __init__(self, pool: Optional[SQLiteConnectionPool] = None) -> None:
        # When an explicit pool is given (tests) it is used for every call.
        # Otherwise the shared per-path pool is resolved on each call so the
        # route-module singleton stays valid across test pool resets.
        self._explicit_pool = pool

    def _pool(self) -> SQLiteConnectionPool:
        if self._explicit_pool is not None:
            return self._explicit_pool
        return get_pool(str(settings.sqlite_path))

    # ── create ──────────────────────────────────────────────────────────────

    def create_artifact(
        self,
        *,
        session_id: int,
        kind: str,
        name: str,
        content: str,
        language: Optional[str] = None,
        message_id: Optional[int] = None,
        turn_id: Optional[str] = None,
        vault_id: Optional[int] = None,
        created_by: Optional[int] = None,
        source_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Insert the artifact row and its version-1 row atomically.

        Version 1 always carries origin='created'. The insert pair runs inside
        one BEGIN IMMEDIATE transaction so no reader can ever observe an
        artifact without its initial version.
        """
        artifact_uid = mint_artifact_uid()
        digest = content_sha256(content)
        refs_json = json.dumps(source_refs or [])

        pool = self._pool()
        conn = pool.get_connection()
        try:
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "INSERT INTO canvas_artifacts "
                    "(artifact_uid, session_id, message_id, turn_id, kind, name, "
                    "language, vault_id, current_version_no, source_refs_json, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        artifact_uid,
                        session_id,
                        message_id,
                        turn_id,
                        kind,
                        name,
                        language,
                        vault_id,
                        refs_json,
                        created_by,
                    ),
                )
                artifact_id = cursor.lastrowid
                if artifact_id is None:
                    raise sqlite3.IntegrityError("canvas artifact insert returned no rowid")
                conn.execute(
                    "INSERT INTO canvas_versions "
                    "(artifact_id, version_no, name, content, content_sha256, origin, "
                    "model_edit_json, created_by) VALUES (?, 1, ?, ?, ?, 'created', NULL, ?)",
                    (artifact_id, name, content, digest, created_by),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            pool.release_connection(conn)

        return self.get_by_uid(artifact_uid) or {}

    # ── reads ───────────────────────────────────────────────────────────────

    def get_by_uid(self, artifact_uid: str) -> Optional[Dict[str, Any]]:
        """Load the artifact row by its public uid, or None."""
        pool = self._pool()
        conn = pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, artifact_uid, session_id, message_id, turn_id, kind, name, "
                "language, vault_id, current_version_no, source_refs_json, created_by, "
                "created_at, updated_at FROM canvas_artifacts WHERE artifact_uid = ?",
                (artifact_uid,),
            )
            row = cursor.fetchone()
            return _row_to_artifact(row) if row is not None else None
        finally:
            pool.release_connection(conn)

    def get_by_id(self, artifact_id: int) -> Optional[Dict[str, Any]]:
        """Load the artifact row by primary key, or None."""
        pool = self._pool()
        conn = pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, artifact_uid, session_id, message_id, turn_id, kind, name, "
                "language, vault_id, current_version_no, source_refs_json, created_by, "
                "created_at, updated_at FROM canvas_artifacts WHERE id = ?",
                (artifact_id,),
            )
            row = cursor.fetchone()
            return _row_to_artifact(row) if row is not None else None
        finally:
            pool.release_connection(conn)

    def list_for_session(self, session_id: int) -> List[Dict[str, Any]]:
        """List a session's artifacts (newest first). No version bodies."""
        pool = self._pool()
        conn = pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, artifact_uid, session_id, message_id, turn_id, kind, name, "
                "language, vault_id, current_version_no, source_refs_json, created_by, "
                "created_at, updated_at FROM canvas_artifacts WHERE session_id = ? "
                "ORDER BY id DESC",
                (session_id,),
            )
            return [_row_to_artifact(row) for row in cursor.fetchall()]
        finally:
            pool.release_connection(conn)

    def list_versions(self, artifact_id: int) -> List[Dict[str, Any]]:
        """List version summaries (NO content bodies), oldest first."""
        pool = self._pool()
        conn = pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, artifact_id, version_no, name, content, content_sha256, "
                "origin, model_edit_json, created_by, created_at "
                "FROM canvas_versions WHERE artifact_id = ? ORDER BY version_no ASC",
                (artifact_id,),
            )
            summaries: List[Dict[str, Any]] = []
            for row in cursor.fetchall():
                summary = _row_to_version(row)
                # Drop the body — the version rail must not ship every version's
                # full content over the wire.
                summary.pop("content", None)
                summaries.append(summary)
            return summaries
        finally:
            pool.release_connection(conn)

    def get_version(self, artifact_id: int, version_no: int) -> Optional[Dict[str, Any]]:
        """Load one exact version (content included), or None."""
        pool = self._pool()
        conn = pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, artifact_id, version_no, name, content, content_sha256, "
                "origin, model_edit_json, created_by, created_at "
                "FROM canvas_versions WHERE artifact_id = ? AND version_no = ?",
                (artifact_id, version_no),
            )
            row = cursor.fetchone()
            return _row_to_version(row) if row is not None else None
        finally:
            pool.release_connection(conn)

    # ── appends ─────────────────────────────────────────────────────────────

    def append_version(
        self,
        artifact_id: int,
        *,
        content: str,
        origin: str,
        name: Optional[str] = None,
        created_by: Optional[int] = None,
        base_version_no: Optional[int] = None,
        force: bool = False,
        model_edit: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Append a new version and bump the artifact's current counter.

        The version insert and the guarded counter update commit atomically in
        one BEGIN IMMEDIATE transaction:

        * ``base_version_no`` given: the counter is bumped via
          ``UPDATE ... SET current_version_no = ? + 1 WHERE id = ? AND
          current_version_no = ?``. A zero rowcount means the base was stale —
          the transaction rolls back and ``CANVAS_VERSION_CONFLICT`` is
          returned (unless ``force=True``, see below).
        * ``base_version_no`` omitted: the current counter is read inside the
          transaction and the version appends at current+1 unconditionally
          (restore/history tooling that does not race a live editor).
        * ``force=True`` with a stale base: instead of failing, the store
          re-reads the current counter and appends at current+1 — history is
          preserved and no version is lost (last-writer-wins, client-initiated).

        ``model_edit`` is persisted as JSON only for origin='model_edit'
        (range/prompt provenance); restores deliberately carry NULL.
        """
        digest = content_sha256(content)
        model_edit_json = (
            json.dumps(model_edit) if origin == "model_edit" and model_edit is not None else None
        )

        new_version_no: Optional[int] = None
        conflict = False
        pool = self._pool()
        conn = pool.get_connection()
        try:
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if base_version_no is not None:
                    cursor = conn.execute(
                        "UPDATE canvas_artifacts SET current_version_no = ? + 1, "
                        "updated_at = (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
                        "WHERE id = ? AND current_version_no = ?",
                        (base_version_no, artifact_id, base_version_no),
                    )
                    if cursor.rowcount == 0:
                        # Stale base: roll the guarded update back. Without
                        # force this is a 409 for the caller; with force the
                        # counter is re-read below and the version appends at
                        # current+1 (history preserved, no version lost).
                        conn.rollback()
                        if not force:
                            conflict = True
                        else:
                            conn.execute("BEGIN IMMEDIATE")
                            row = conn.execute(
                                "SELECT current_version_no FROM canvas_artifacts WHERE id = ?",
                                (artifact_id,),
                            ).fetchone()
                            if row is None:
                                conflict = True
                            else:
                                new_version_no = int(row[0]) + 1

                if not conflict and new_version_no is None:
                    if base_version_no is None:
                        row = conn.execute(
                            "SELECT current_version_no FROM canvas_artifacts WHERE id = ?",
                            (artifact_id,),
                        ).fetchone()
                        if row is None:
                            conflict = True
                        else:
                            new_version_no = int(row[0]) + 1
                    else:
                        new_version_no = base_version_no + 1

                if not conflict and new_version_no is not None:
                    conn.execute(
                        "INSERT INTO canvas_versions "
                        "(artifact_id, version_no, name, content, content_sha256, "
                        "origin, model_edit_json, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            artifact_id,
                            new_version_no,
                            name,
                            content,
                            digest,
                            origin,
                            model_edit_json,
                            created_by,
                        ),
                    )
                    if base_version_no is None or force:
                        # Unconditional counter sync for the no-base / force
                        # paths (the guarded path already bumped it).
                        conn.execute(
                            "UPDATE canvas_artifacts SET current_version_no = ?, "
                            "updated_at = (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
                            "WHERE id = ?",
                            (new_version_no, artifact_id),
                        )
                    conn.commit()
                else:
                    conn.rollback()
            except Exception:
                conn.rollback()
                raise
        finally:
            pool.release_connection(conn)

        if conflict:
            return CANVAS_VERSION_CONFLICT
        return self.get_version(artifact_id, int(new_version_no))
