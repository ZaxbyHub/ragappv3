"""
TagStore: vault-scoped CRUD for document organization tags and their
assignment to files (document_tags join).

All operations are vault-scoped. Tag names are unique per vault (case-sensitive
at the storage level; callers normalize whitespace). Assignment helpers operate
only on files that belong to the same vault as the tag, so a tag can never be
attached to a document in another vault.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Tag:
    id: int
    vault_id: int
    name: str
    color: str
    created_at: str
    updated_at: str
    document_count: int = 0


def _to_tag(row: sqlite3.Row) -> Tag:
    d = dict(row)
    return Tag(
        id=d["id"],
        vault_id=d["vault_id"],
        name=d["name"],
        color=d.get("color") or "",
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        document_count=d.get("document_count") or 0,
    )


class TagDuplicateError(Exception):
    """Raised when a tag name already exists within the vault."""


class TagStore:
    """Vault-scoped CRUD for tags + document_tags assignment."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # -----------------------------------------------------------------------
    # Tag CRUD
    # -----------------------------------------------------------------------

    def create_tag(self, vault_id: int, name: str, color: str = "") -> Tag:
        name = name.strip()
        if not name:
            raise ValueError("Tag name must not be empty")
        now = datetime.utcnow().isoformat()
        try:
            cur = self._db.execute(
                """INSERT INTO tags (vault_id, name, color, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (vault_id, name, color, now, now),
            )
        except sqlite3.IntegrityError as e:
            self._db.rollback()
            raise TagDuplicateError(
                f"Tag {name!r} already exists in this vault"
            ) from e
        self._db.commit()
        return self.get_tag(cur.lastrowid, vault_id)  # type: ignore[return-value]

    def get_tag(self, tag_id: int, vault_id: int) -> Optional[Tag]:
        row = self._db.execute(
            """SELECT t.*,
                      (SELECT COUNT(*) FROM document_tags dt WHERE dt.tag_id = t.id)
                          AS document_count
               FROM tags t WHERE t.id = ? AND t.vault_id = ?""",
            (tag_id, vault_id),
        ).fetchone()
        return _to_tag(row) if row else None

    def list_tags(self, vault_id: int) -> list[Tag]:
        rows = self._db.execute(
            """SELECT t.*,
                      (SELECT COUNT(*) FROM document_tags dt WHERE dt.tag_id = t.id)
                          AS document_count
               FROM tags t WHERE t.vault_id = ?
               ORDER BY t.name COLLATE NOCASE ASC""",
            (vault_id,),
        ).fetchall()
        return [_to_tag(r) for r in rows]

    def update_tag(
        self,
        tag_id: int,
        vault_id: int,
        name: Optional[str] = None,
        color: Optional[str] = None,
    ) -> Optional[Tag]:
        updates: dict = {}
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Tag name must not be empty")
            updates["name"] = name
        if color is not None:
            updates["color"] = color
        if not updates:
            return self.get_tag(tag_id, vault_id)
        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [tag_id, vault_id]
        try:
            cur = self._db.execute(
                f"UPDATE tags SET {set_clause} WHERE id = ? AND vault_id = ?", values
            )
        except sqlite3.IntegrityError as e:
            self._db.rollback()
            raise TagDuplicateError(
                f"Tag {updates.get('name')!r} already exists in this vault"
            ) from e
        if cur.rowcount == 0:
            return None
        self._db.commit()
        return self.get_tag(tag_id, vault_id)

    def delete_tag(self, tag_id: int, vault_id: int) -> bool:
        # document_tags rows are removed via ON DELETE CASCADE.
        cur = self._db.execute(
            "DELETE FROM tags WHERE id = ? AND vault_id = ?", (tag_id, vault_id)
        )
        self._db.commit()
        return cur.rowcount > 0

    # -----------------------------------------------------------------------
    # Assignment
    # -----------------------------------------------------------------------

    def _vault_tag_ids(self, vault_id: int, tag_ids: list[int]) -> list[int]:
        """Return the subset of tag_ids that belong to vault_id."""
        if not tag_ids:
            return []
        placeholders = ",".join("?" * len(tag_ids))
        rows = self._db.execute(
            f"SELECT id FROM tags WHERE vault_id = ? AND id IN ({placeholders})",
            (vault_id, *tag_ids),
        ).fetchall()
        return [r["id"] for r in rows]

    def _vault_file_ids(self, vault_id: int, file_ids: list[int]) -> list[int]:
        """Return the subset of file_ids that belong to vault_id."""
        if not file_ids:
            return []
        placeholders = ",".join("?" * len(file_ids))
        rows = self._db.execute(
            f"SELECT id FROM files WHERE vault_id = ? AND id IN ({placeholders})",
            (vault_id, *file_ids),
        ).fetchall()
        return [r["id"] for r in rows]

    def assign_tags(
        self, vault_id: int, file_ids: list[int], tag_ids: list[int]
    ) -> int:
        """Add (file, tag) assignments for the given files and tags.

        Both files and tags are filtered to the vault; cross-vault references are
        silently dropped so a caller cannot tag a document in another vault.
        Existing assignments are left intact (INSERT OR IGNORE). Returns the
        number of new assignment rows created.
        """
        valid_files = self._vault_file_ids(vault_id, file_ids)
        valid_tags = self._vault_tag_ids(vault_id, tag_ids)
        if not valid_files or not valid_tags:
            return 0
        now = datetime.utcnow().isoformat()
        created = 0
        for fid in valid_files:
            for tid in valid_tags:
                cur = self._db.execute(
                    """INSERT OR IGNORE INTO document_tags (file_id, tag_id, created_at)
                       VALUES (?, ?, ?)""",
                    (fid, tid, now),
                )
                created += cur.rowcount
        self._db.commit()
        return created

    def unassign_tag(self, vault_id: int, file_id: int, tag_id: int) -> bool:
        """Remove a single (file, tag) assignment. Vault-scoped on both sides."""
        if not self._vault_file_ids(vault_id, [file_id]):
            return False
        if not self._vault_tag_ids(vault_id, [tag_id]):
            return False
        cur = self._db.execute(
            "DELETE FROM document_tags WHERE file_id = ? AND tag_id = ?",
            (file_id, tag_id),
        )
        self._db.commit()
        return cur.rowcount > 0

    def set_document_tags(
        self, vault_id: int, file_id: int, tag_ids: list[int]
    ) -> list[Tag]:
        """Replace the full tag set for one document. Returns the new tags."""
        if not self._vault_file_ids(vault_id, [file_id]):
            return []
        valid_tags = self._vault_tag_ids(vault_id, tag_ids)
        now = datetime.utcnow().isoformat()
        self._db.execute(
            "DELETE FROM document_tags WHERE file_id = ?", (file_id,)
        )
        for tid in valid_tags:
            self._db.execute(
                """INSERT OR IGNORE INTO document_tags (file_id, tag_id, created_at)
                   VALUES (?, ?, ?)""",
                (file_id, tid, now),
            )
        self._db.commit()
        return self.get_tags_for_document(file_id)

    def get_tags_for_document(self, file_id: int) -> list[Tag]:
        # Defense-in-depth: only return tags that belong to the file's own
        # vault. Assignment already enforces this, but the extra join guards
        # against any future path that links a cross-vault tag (F-005).
        rows = self._db.execute(
            """SELECT t.*, 0 AS document_count
               FROM tags t
               JOIN document_tags dt ON dt.tag_id = t.id
               JOIN files f ON f.id = dt.file_id
               WHERE dt.file_id = ? AND t.vault_id = f.vault_id
               ORDER BY t.name COLLATE NOCASE ASC""",
            (file_id,),
        ).fetchall()
        return [_to_tag(r) for r in rows]

    def get_tags_for_documents(
        self, file_ids: list[int]
    ) -> dict[int, list[Tag]]:
        """Batch-fetch tags for many files. Returns {file_id: [Tag, ...]}."""
        result: dict[int, list[Tag]] = {fid: [] for fid in file_ids}
        if not file_ids:
            return result
        placeholders = ",".join("?" * len(file_ids))
        # See get_tags_for_document: scope each file's tags to its own vault.
        rows = self._db.execute(
            f"""SELECT dt.file_id AS _fid, t.*, 0 AS document_count
                FROM tags t
                JOIN document_tags dt ON dt.tag_id = t.id
                JOIN files f ON f.id = dt.file_id
                WHERE dt.file_id IN ({placeholders}) AND t.vault_id = f.vault_id
                ORDER BY t.name COLLATE NOCASE ASC""",
            tuple(file_ids),
        ).fetchall()
        for row in rows:
            d = dict(row)
            fid = d.pop("_fid")
            result.setdefault(fid, []).append(_to_tag(row))
        return result

    def file_ids_for_tag(self, vault_id: int, tag_id: int) -> list[int]:
        """Return file ids in the vault assigned the given tag."""
        rows = self._db.execute(
            """SELECT dt.file_id
               FROM document_tags dt
               JOIN files f ON f.id = dt.file_id
               WHERE dt.tag_id = ? AND f.vault_id = ?""",
            (tag_id, vault_id),
        ).fetchall()
        return [r["file_id"] for r in rows]


__all__ = ["Tag", "TagStore", "TagDuplicateError"]
