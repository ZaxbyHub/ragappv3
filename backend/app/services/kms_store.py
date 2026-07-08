"""
KMSStore: CRUD + FTS search for user-curated Knowledge Management entries,
plus the kms_compile_jobs queue.

All operations are vault-scoped. Slug normalization is enforced on create and
on title/slug updates; slug collisions within a vault are resolved by appending
a numeric suffix. FTS search is backed by kms_entries_fts.

The job methods mirror WikiStore's compile-job API so KMSCompileProcessor can
follow the same poll / claim / complete / fail / retry lifecycle.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from app.services.wiki_store import normalize_slug

# ---------------------------------------------------------------------------
# DTO dataclasses
# ---------------------------------------------------------------------------


@dataclass
class KMSEntry:
    id: int
    vault_id: int
    file_id: Optional[int]
    slug: str
    title: str
    body: str
    summary: str
    tags_json: str
    source_type: str
    status: str
    created_by: Optional[int]
    created_at: str
    updated_at: str
    last_compiled_at: Optional[str]

    @property
    def tags(self) -> list:
        try:
            return json.loads(self.tags_json)
        except (json.JSONDecodeError, TypeError):
            return []


@dataclass
class KMSCompileJob:
    id: int
    vault_id: int
    trigger_type: str
    trigger_id: Optional[str]
    status: str
    error: Optional[str]
    result_json: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    input_json: Optional[str] = None
    retry_count: int = 0


# ---------------------------------------------------------------------------
# Row -> DTO helpers
# ---------------------------------------------------------------------------


def _to_entry(row: sqlite3.Row) -> KMSEntry:
    d = dict(row)
    return KMSEntry(
        id=d["id"],
        vault_id=d["vault_id"],
        file_id=d.get("file_id"),
        slug=d["slug"],
        title=d["title"],
        body=d["body"] or "",
        summary=d["summary"] or "",
        tags_json=d.get("tags_json") or "[]",
        source_type=d["source_type"],
        status=d["status"],
        created_by=d.get("created_by"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        last_compiled_at=d.get("last_compiled_at"),
    )


def _to_job(row: sqlite3.Row) -> KMSCompileJob:
    d = dict(row)
    return KMSCompileJob(
        id=d["id"],
        vault_id=d["vault_id"],
        trigger_type=d["trigger_type"],
        trigger_id=d.get("trigger_id"),
        status=d["status"],
        error=d.get("error"),
        result_json=d.get("result_json") or "{}",
        created_at=d["created_at"],
        started_at=d.get("started_at"),
        completed_at=d.get("completed_at"),
        input_json=d.get("input_json"),
        retry_count=d.get("retry_count") or 0,
    )


# ---------------------------------------------------------------------------
# KMSStore
# ---------------------------------------------------------------------------


class KMSStore:
    """Vault-scoped CRUD and FTS search for kms_entries + kms_compile_jobs."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # -----------------------------------------------------------------------
    # Slug helpers
    # -----------------------------------------------------------------------

    def _unique_slug(self, vault_id: int, base: str, exclude_id: Optional[int] = None) -> str:
        """Return a vault-unique slug derived from ``base``.

        Appends -2, -3, … on collision. ``exclude_id`` lets an update keep its
        own slug without colliding with itself.
        """
        slug = normalize_slug(base) or "entry"
        candidate = slug
        n = 1
        while True:
            row = self._db.execute(
                "SELECT id FROM kms_entries WHERE vault_id = ? AND slug = ?",
                (vault_id, candidate),
            ).fetchone()
            if row is None or (exclude_id is not None and dict(row)["id"] == exclude_id):
                return candidate
            n += 1
            candidate = f"{slug}-{n}"

    # -----------------------------------------------------------------------
    # Entries
    # -----------------------------------------------------------------------

    def create_entry(
        self,
        vault_id: int,
        title: str,
        body: str = "",
        summary: str = "",
        tags: Optional[list] = None,
        slug: Optional[str] = None,
        file_id: Optional[int] = None,
        source_type: str = "manual",
        status: str = "draft",
        created_by: Optional[int] = None,
    ) -> KMSEntry:
        now = datetime.utcnow().isoformat()
        slug = self._unique_slug(vault_id, slug or title)
        tags_json = json.dumps(tags or [])
        cur = self._db.execute(
            """
            INSERT INTO kms_entries
                (vault_id, file_id, slug, title, body, summary, tags_json,
                 source_type, status, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (vault_id, file_id, slug, title, body, summary, tags_json,
             source_type, status, created_by, now, now),
        )
        self._db.commit()
        return self.get_entry(cur.lastrowid)  # type: ignore[return-value]

    def get_entry(self, entry_id: int) -> Optional[KMSEntry]:
        row = self._db.execute(
            "SELECT * FROM kms_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _to_entry(row) if row else None

    def get_entry_by_file(
        self, vault_id: int, file_id: int, source_type: str = "document"
    ) -> Optional[KMSEntry]:
        """Return the document-sourced entry for a file, or None."""
        row = self._db.execute(
            "SELECT * FROM kms_entries WHERE vault_id = ? AND file_id = ? AND source_type = ? LIMIT 1",
            (vault_id, file_id, source_type),
        ).fetchone()
        return _to_entry(row) if row else None

    def list_entries(
        self,
        vault_id: int,
        status: Optional[str] = None,
        tag: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[KMSEntry]:
        offset = (page - 1) * per_page
        params: list[Any] = []
        if search:
            ids = self._fts_entry_ids(search)
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            sql = f"SELECT * FROM kms_entries WHERE id IN ({placeholders}) AND vault_id = ?"
            params = [*ids, vault_id]
        else:
            sql = "SELECT * FROM kms_entries WHERE vault_id = ?"
            params = [vault_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if tag:
            # Membership test via json_each avoids LIKE false-positives.
            sql += " AND EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value = ?)"
            params.append(tag)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params += [per_page, offset]
        rows = self._db.execute(sql, params).fetchall()
        return [_to_entry(r) for r in rows]

    def count_entries(
        self,
        vault_id: int,
        status: Optional[str] = None,
        tag: Optional[str] = None,
        search: Optional[str] = None,
    ) -> int:
        params: list[Any] = []
        if search:
            ids = self._fts_entry_ids(search)
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            sql = f"SELECT COUNT(*) FROM kms_entries WHERE id IN ({placeholders}) AND vault_id = ?"
            params = [*ids, vault_id]
        else:
            sql = "SELECT COUNT(*) FROM kms_entries WHERE vault_id = ?"
            params = [vault_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if tag:
            sql += " AND EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value = ?)"
            params.append(tag)
        return int(self._db.execute(sql, params).fetchone()[0])

    def update_entry(self, entry_id: int, vault_id: int, **kwargs: Any) -> Optional[KMSEntry]:
        allowed = {"title", "body", "summary", "tags", "slug", "status", "last_compiled_at"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_entry(entry_id)
        if "tags" in updates:
            updates["tags_json"] = json.dumps(updates.pop("tags") or [])
        # A provided-but-blank slug must never be persisted: slug is NOT NULL and
        # an empty value would collide on the next blank-slug update. Drop it so it
        # is regenerated from the title (if also being updated) or left unchanged.
        if "slug" in updates and not str(updates["slug"] or "").strip():
            del updates["slug"]
        if "slug" in updates:
            updates["slug"] = self._unique_slug(vault_id, updates["slug"], exclude_id=entry_id)
        elif "title" in updates:
            updates["slug"] = self._unique_slug(vault_id, updates["title"], exclude_id=entry_id)
        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [entry_id, vault_id]
        cur = self._db.execute(
            f"UPDATE kms_entries SET {set_clause} WHERE id = ? AND vault_id = ?", values
        )
        if cur.rowcount == 0:
            return None
        self._db.commit()
        return self.get_entry(entry_id)

    def delete_entry(self, entry_id: int, vault_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM kms_entries WHERE id = ? AND vault_id = ?", (entry_id, vault_id)
        )
        self._db.commit()
        return cur.rowcount > 0

    def upsert_document_entry(
        self,
        vault_id: int,
        file_id: int,
        title: str,
        body: str,
        summary: str = "",
    ) -> KMSEntry:
        """Create or update the document-sourced entry for a file (idempotent).

        Used by the compile-on-ingest hook. Preserves a user-chosen status and
        tags on re-compile; only refreshes title/body/summary and bumps
        last_compiled_at.
        """
        now = datetime.utcnow().isoformat()
        existing = self.get_entry_by_file(vault_id, file_id, source_type="document")
        if existing:
            self._db.execute(
                """UPDATE kms_entries
                   SET title = ?, body = ?, summary = ?, updated_at = ?, last_compiled_at = ?
                   WHERE id = ?""",
                (title, body, summary, now, now, existing.id),
            )
            self._db.commit()
            return self.get_entry(existing.id)  # type: ignore[return-value]
        slug = self._unique_slug(vault_id, title)
        cur = self._db.execute(
            """INSERT INTO kms_entries
               (vault_id, file_id, slug, title, body, summary, tags_json,
                source_type, status, created_by, created_at, updated_at, last_compiled_at)
               VALUES (?, ?, ?, ?, ?, ?, '[]', 'document', 'draft', NULL, ?, ?, ?)""",
            (vault_id, file_id, slug, title, body, summary, now, now, now),
        )
        self._db.commit()
        return self.get_entry(cur.lastrowid)  # type: ignore[return-value]

    def _fts_entry_ids(self, query: str) -> list[int]:
        try:
            rows = self._db.execute(
                "SELECT rowid FROM kms_entries_fts WHERE kms_entries_fts MATCH ? ORDER BY rank",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed FTS5 query (unbalanced quotes etc.) — treat as no match.
            return []
        return [r[0] for r in rows]

    # -----------------------------------------------------------------------
    # Compile Jobs (mirror WikiStore lifecycle)
    # -----------------------------------------------------------------------

    def create_job(
        self,
        vault_id: int,
        trigger_type: str,
        trigger_id: Optional[str] = None,
        input_json: Optional[Any] = None,
    ) -> KMSCompileJob:
        now = datetime.utcnow().isoformat()
        if isinstance(input_json, dict):
            input_json = json.dumps(input_json)
        cur = self._db.execute(
            """INSERT INTO kms_compile_jobs (vault_id, trigger_type, trigger_id, status, input_json, created_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (vault_id, trigger_type, trigger_id, input_json or "{}", now),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT * FROM kms_compile_jobs WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _to_job(row)

    def list_jobs(self, vault_id: int, status: Optional[str] = None) -> list[KMSCompileJob]:
        if status:
            rows = self._db.execute(
                "SELECT * FROM kms_compile_jobs WHERE vault_id = ? AND status = ? ORDER BY created_at DESC",
                (vault_id, status),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM kms_compile_jobs WHERE vault_id = ? ORDER BY created_at DESC",
                (vault_id,),
            ).fetchall()
        return [_to_job(r) for r in rows]

    def get_job(self, job_id: int, vault_id: int) -> Optional[KMSCompileJob]:
        row = self._db.execute(
            "SELECT * FROM kms_compile_jobs WHERE id = ? AND vault_id = ?",
            (job_id, vault_id),
        ).fetchone()
        return _to_job(row) if row else None

    def claim_next_pending_job(self) -> Optional[KMSCompileJob]:
        """Atomically claim the oldest pending job. Returns it or None.

        Uses BEGIN IMMEDIATE so concurrent workers cannot claim the same row.
        """
        now = datetime.utcnow().isoformat()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT * FROM kms_compile_jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                self._db.rollback()
                return None
            job_id = dict(row)["id"]
            self._db.execute(
                "UPDATE kms_compile_jobs SET status = 'running', started_at = ? WHERE id = ?",
                (now, job_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        row = self._db.execute(
            "SELECT * FROM kms_compile_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _to_job(row) if row else None

    def complete_job(self, job_id: int, result_json: Any) -> None:
        """Mark job completed. No-op if the job was already cancelled."""
        row = self._db.execute(
            "SELECT status FROM kms_compile_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row and dict(row)["status"] == "cancelled":
            return
        now = datetime.utcnow().isoformat()
        if isinstance(result_json, dict):
            result_json = json.dumps(result_json)
        self._db.execute(
            "UPDATE kms_compile_jobs SET status = 'completed', completed_at = ?, result_json = ? WHERE id = ?",
            (now, result_json or "{}", job_id),
        )
        self._db.commit()

    def fail_job(self, job_id: int, error: str) -> int:
        """Mark job failed, increment retry_count. Returns new retry_count.

        No-op if the job is already cancelled.
        """
        now = datetime.utcnow().isoformat()
        self._db.execute(
            """UPDATE kms_compile_jobs
               SET status = 'failed', completed_at = ?, error = ?, retry_count = retry_count + 1
               WHERE id = ? AND status != 'cancelled'""",
            (now, error[:2000], job_id),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT retry_count FROM kms_compile_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row)["retry_count"] if row else 0

    def reset_job_to_pending(self, job_id: int) -> None:
        """Reset a failed job back to pending for auto-retry by the processor.

        Guarded by ``status = 'failed'`` so the auto-retry path cannot clobber a
        job that has since been retried-then-cancelled by the user during the
        processor's backoff window (issue #276 A6-1, mirrors the wiki_store
        fix). The processor only calls this immediately after ``fail_job`` set
        status='failed', so the guard is a no-op on the intended path.
        """
        self._db.execute(
            "UPDATE kms_compile_jobs SET status = 'pending', started_at = NULL, completed_at = NULL "
            "WHERE id = ? AND status = 'failed'",
            (job_id,),
        )
        self._db.commit()

    def cancel_job(self, job_id: int, vault_id: int) -> bool:
        """Cancel a pending or running job. Returns True if cancelled."""
        row = self._db.execute(
            "SELECT status FROM kms_compile_jobs WHERE id = ? AND vault_id = ?",
            (job_id, vault_id),
        ).fetchone()
        if not row or dict(row)["status"] not in ("pending", "running"):
            return False
        self._db.execute(
            "UPDATE kms_compile_jobs SET status = 'cancelled', completed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), job_id),
        )
        self._db.commit()
        return True

    def retry_job(self, job_id: int, vault_id: int) -> Optional[KMSCompileJob]:
        """Reset a failed job to pending. Returns the updated job or None."""
        row = self._db.execute(
            "SELECT status FROM kms_compile_jobs WHERE id = ? AND vault_id = ?",
            (job_id, vault_id),
        ).fetchone()
        if not row or dict(row)["status"] != "failed":
            return None
        self._db.execute(
            "UPDATE kms_compile_jobs SET status = 'pending', error = NULL, started_at = NULL, completed_at = NULL WHERE id = ?",
            (job_id,),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT * FROM kms_compile_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _to_job(row) if row else None

    def reset_running_jobs(self) -> int:
        """Reset jobs stuck in 'running' to 'pending' on processor startup.

        Returns the number of jobs reset (orphans from a previous crash).
        """
        cur = self._db.execute(
            "UPDATE kms_compile_jobs SET status = 'pending', started_at = NULL WHERE status = 'running'"
        )
        self._db.commit()
        return cur.rowcount


__all__ = ["KMSEntry", "KMSCompileJob", "KMSStore"]
