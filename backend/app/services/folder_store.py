"""
FolderStore: vault-scoped CRUD for the document folder hierarchy and moving
documents between folders.

All operations are vault-scoped. Folder names are unique within their parent
(enforced here because SQLite treats NULL parents as distinct in UNIQUE
constraints). Reparenting rejects cycles — a folder can never become its own
descendant. Moving documents only affects files that belong to the same vault
as the target folder, so a document can never be filed into another vault's
folder.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from app.services.store_utils import vault_file_ids as _vault_file_ids_shared

# Sentinel distinguishing "parent not provided" from "parent explicitly set to
# NULL (move to root)" in update_folder.
_UNSET: Any = object()


@dataclass
class Folder:
    id: int
    vault_id: int
    parent_folder_id: Optional[int]
    name: str
    description: str
    created_at: str
    updated_at: str
    document_count: int = 0


def _to_folder(row: sqlite3.Row) -> Folder:
    d = dict(row)
    return Folder(
        id=d["id"],
        vault_id=d["vault_id"],
        parent_folder_id=d["parent_folder_id"],
        name=d["name"],
        description=d.get("description") or "",
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        document_count=d.get("document_count") or 0,
    )


class FolderDuplicateError(Exception):
    """Raised when a folder name already exists within the same parent."""


class FolderCycleError(Exception):
    """Raised when a reparent would make a folder its own descendant."""


class FolderNotFoundError(Exception):
    """Raised when a referenced folder doesn't exist in the vault."""


class FolderStore:
    """Vault-scoped CRUD for folders + moving documents between folders."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _name_exists(
        self,
        vault_id: int,
        parent_folder_id: Optional[int],
        name: str,
        exclude_id: Optional[int] = None,
    ) -> bool:
        """True if a sibling folder with the same name already exists.

        NULL parents are matched with IS NULL so root-level names are also
        deduplicated (a plain UNIQUE index would treat NULL parents as
        distinct and let duplicates through).
        """
        if parent_folder_id is None:
            sql = (
                "SELECT id FROM folders WHERE vault_id = ? "
                "AND parent_folder_id IS NULL AND name = ? COLLATE NOCASE"
            )
            params: list = [vault_id, name]
        else:
            sql = (
                "SELECT id FROM folders WHERE vault_id = ? "
                "AND parent_folder_id = ? AND name = ? COLLATE NOCASE"
            )
            params = [vault_id, parent_folder_id, name]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        return self._db.execute(sql, params).fetchone() is not None

    def _require_folder_in_vault(self, vault_id: int, folder_id: int) -> None:
        row = self._db.execute(
            "SELECT id FROM folders WHERE id = ? AND vault_id = ?",
            (folder_id, vault_id),
        ).fetchone()
        if not row:
            raise FolderNotFoundError("Folder not found in this vault")

    def _descendant_ids(self, vault_id: int, folder_id: int) -> set[int]:
        """Return all descendant folder ids (children, grandchildren, ...)."""
        result: set[int] = set()
        frontier = [folder_id]
        while frontier:
            placeholders = ",".join("?" * len(frontier))
            rows = self._db.execute(
                f"""SELECT id FROM folders
                    WHERE vault_id = ? AND parent_folder_id IN ({placeholders})""",
                (vault_id, *frontier),
            ).fetchall()
            children = [r["id"] for r in rows if r["id"] not in result]
            result.update(children)
            frontier = children
        return result

    def _vault_file_ids(self, vault_id: int, file_ids: list[int]) -> list[int]:
        """Return the subset of file_ids that belong to vault_id.

        Delegates to the shared helper (store_utils.vault_file_ids) so the
        implementation is not duplicated across FolderStore/TagStore (F3-3).
        """
        return _vault_file_ids_shared(self._db, vault_id, file_ids)

    # -----------------------------------------------------------------------
    # Folder CRUD
    # -----------------------------------------------------------------------

    def create_folder(
        self,
        vault_id: int,
        name: str,
        description: str = "",
        parent_folder_id: Optional[int] = None,
    ) -> Folder:
        name = name.strip()
        if not name:
            raise ValueError("Folder name must not be empty")
        if parent_folder_id is not None:
            self._require_folder_in_vault(vault_id, parent_folder_id)
        # BEGIN IMMEDIATE: name-uniqueness check + INSERT must be atomic so two
        # concurrent creates cannot both pass the check before either commits.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            if self._name_exists(vault_id, parent_folder_id, name):
                raise FolderDuplicateError(
                    f"Folder {name!r} already exists in this location"
                )
            now = datetime.utcnow().isoformat()
            cur = self._db.execute(
                """INSERT INTO folders
                       (vault_id, parent_folder_id, name, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (vault_id, parent_folder_id, name, description, now, now),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_folder(cur.lastrowid, vault_id)  # type: ignore[return-value]

    def get_folder(self, folder_id: int, vault_id: int) -> Optional[Folder]:
        row = self._db.execute(
            """SELECT f.*,
                      (SELECT COUNT(*) FROM files fi WHERE fi.folder_id = f.id)
                          AS document_count
               FROM folders f WHERE f.id = ? AND f.vault_id = ?""",
            (folder_id, vault_id),
        ).fetchone()
        return _to_folder(row) if row else None

    def list_folders(self, vault_id: int) -> list[Folder]:
        rows = self._db.execute(
            """SELECT f.*,
                      (SELECT COUNT(*) FROM files fi WHERE fi.folder_id = f.id)
                          AS document_count
               FROM folders f WHERE f.vault_id = ?
               ORDER BY f.name COLLATE NOCASE ASC""",
            (vault_id,),
        ).fetchall()
        return [_to_folder(r) for r in rows]

    def update_folder(
        self,
        folder_id: int,
        vault_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parent_folder_id: Any = _UNSET,
    ) -> Optional[Folder]:
        current = self.get_folder(folder_id, vault_id)
        if not current:
            return None

        updates: dict = {}
        new_parent = current.parent_folder_id
        if parent_folder_id is not _UNSET:
            if parent_folder_id == folder_id:
                raise FolderCycleError("A folder cannot be its own parent")
            if parent_folder_id is not None:
                self._require_folder_in_vault(vault_id, parent_folder_id)
                if parent_folder_id in self._descendant_ids(vault_id, folder_id):
                    raise FolderCycleError(
                        "Cannot move a folder into its own descendant"
                    )
            updates["parent_folder_id"] = parent_folder_id
            new_parent = parent_folder_id

        new_name = current.name
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Folder name must not be empty")
            updates["name"] = name
            new_name = name

        if description is not None:
            updates["description"] = description

        if not updates:
            return current

        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [folder_id, vault_id]

        if "name" in updates or "parent_folder_id" in updates:
            # BEGIN IMMEDIATE: name-uniqueness check + UPDATE must be atomic so two
            # concurrent renames/reparents cannot both pass the check before either
            # commits.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._name_exists(
                    vault_id, new_parent, new_name, exclude_id=folder_id
                ):
                    raise FolderDuplicateError(
                        f"Folder {new_name!r} already exists in this location"
                    )
                self._db.execute(
                    f"UPDATE folders SET {set_clause} WHERE id = ? AND vault_id = ?",
                    values,
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        else:
            self._db.execute(
                f"UPDATE folders SET {set_clause} WHERE id = ? AND vault_id = ?",
                values,
            )
            self._db.commit()
        return self.get_folder(folder_id, vault_id)

    def delete_folder(self, folder_id: int, vault_id: int) -> bool:
        """Delete a folder. Subfolders cascade-delete; documents in the folder
        (and its subfolders) become unfiled via ON DELETE SET NULL."""
        cur = self._db.execute(
            "DELETE FROM folders WHERE id = ? AND vault_id = ?",
            (folder_id, vault_id),
        )
        self._db.commit()
        return cur.rowcount > 0

    # -----------------------------------------------------------------------
    # Document assignment
    # -----------------------------------------------------------------------

    def move_documents(
        self, vault_id: int, file_ids: list[int], folder_id: Optional[int]
    ) -> int:
        """Move documents into a folder (or to root when folder_id is None).

        Both the target folder and the files are scoped to the vault; files
        from another vault are silently dropped. Returns the number of files
        moved. Raises FolderNotFoundError when the target folder isn't in the
        vault.
        """
        if folder_id is not None:
            self._require_folder_in_vault(vault_id, folder_id)
        valid_files = self._vault_file_ids(vault_id, file_ids)
        if not valid_files:
            return 0
        placeholders = ",".join("?" * len(valid_files))
        cur = self._db.execute(
            f"UPDATE files SET folder_id = ? WHERE id IN ({placeholders})",
            (folder_id, *valid_files),
        )
        self._db.commit()
        return cur.rowcount


__all__ = [
    "Folder",
    "FolderStore",
    "FolderDuplicateError",
    "FolderCycleError",
    "FolderNotFoundError",
]
