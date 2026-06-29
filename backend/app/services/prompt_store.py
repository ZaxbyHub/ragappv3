"""PromptVersionStore: CRUD for prompt_versions table (FR-007)."""

import sqlite3
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class PromptVersion:
    """A prompt version row."""

    id: int
    version: str
    content: str
    created_at: str
    is_active: bool
    created_by: Optional[str]


class PromptVersionStore:
    """Synchronous CRUD store for prompt_versions."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_active(self) -> Optional[PromptVersion]:
        """Return the currently-active prompt version, or None."""
        row = self._db.execute(
            "SELECT id, version, content, created_at, is_active, created_by "
            "FROM prompt_versions WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_prompt_version(row)

    def list_versions(self) -> List[PromptVersion]:
        """Return all versions ordered by created_at DESC (metadata only)."""
        rows = self._db.execute(
            "SELECT id, version, content, created_at, is_active, created_by "
            "FROM prompt_versions ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_prompt_version(r) for r in rows]

    def get_version(self, version: str) -> Optional[PromptVersion]:
        """Return a specific version by name, or None."""
        row = self._db.execute(
            "SELECT id, version, content, created_at, is_active, created_by "
            "FROM prompt_versions WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_prompt_version(row)

    def get_for_org(self, org_id: int) -> Optional[PromptVersion]:
        """Return the org's override version if set, else None.

        Does NOT fall back to the global active — use :meth:`resolve_for_org`
        when the effective version is needed.
        """
        row = self._db.execute(
            """SELECT pv.id, pv.version, pv.content, pv.created_at,
                      pv.is_active, pv.created_by
               FROM prompt_org_overrides poo
               JOIN prompt_versions pv ON pv.version = poo.version
               WHERE poo.org_id = ?""",
            (org_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_prompt_version(row)

    def resolve_for_org(self, org_id: int) -> Optional[PromptVersion]:
        """Return the effective prompt version for an org.

        Org override if set, else the global active version, else None.
        """
        override = self.get_for_org(org_id)
        if override is not None:
            return override
        return self.get_active()

    # ------------------------------------------------------------------
    # Org-override mutations
    # ------------------------------------------------------------------

    def set_org_override(
        self,
        org_id: int,
        version: str,
        set_by: Optional[str] = None,
    ) -> PromptVersion:
        """Set (upsert) an org's prompt version override.

        Validates that ``version`` exists in prompt_versions before writing.
        Returns the overridden PromptVersion.
        """
        # Validate version exists
        pv = self.get_version(version)
        if pv is None:
            raise ValueError(f"No prompt version with version={version!r}")

        self._db.execute(
            """INSERT INTO prompt_org_overrides (org_id, version, set_by)
               VALUES (?, ?, ?)
               ON CONFLICT(org_id) DO UPDATE SET
                   version = excluded.version,
                   set_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                   set_by = excluded.set_by""",
            (org_id, version, set_by),
        )
        self._db.commit()
        # Return the version row (not the override row)
        return pv

    def clear_org_override(self, org_id: int) -> None:
        """Delete the org's prompt override, if any. Idempotent."""
        self._db.execute(
            "DELETE FROM prompt_org_overrides WHERE org_id = ?",
            (org_id,),
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create_version(
        self,
        version: str,
        content: str,
        *,
        activate: bool = False,
        created_by: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> PromptVersion:
        """Create a new prompt version.

        If activate=True, this version becomes the active one immediately.
        """
        if created_at is not None:
            cursor = self._db.execute(
                "INSERT INTO prompt_versions (version, content, is_active, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (version, content, 1 if activate else 0, created_by, created_at),
            )
        else:
            cursor = self._db.execute(
                "INSERT INTO prompt_versions (version, content, is_active, created_by) "
                "VALUES (?, ?, ?, ?)",
                (version, content, 1 if activate else 0, created_by),
            )
        self._db.commit()
        row = self._db.execute(
            "SELECT id, version, content, created_at, is_active, created_by "
            "FROM prompt_versions WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return self._row_to_prompt_version(row)

    def activate(self, version: str) -> PromptVersion:
        """Activate a specific version (transactionally ensures exactly one active)."""
        row = self._db.execute(
            "SELECT id, version, content, created_at, is_active, created_by "
            "FROM prompt_versions WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No prompt version with version={version!r}")

        self._db.execute("UPDATE prompt_versions SET is_active = 0")
        self._db.execute(
            "UPDATE prompt_versions SET is_active = 1 WHERE version = ?",
            (version,),
        )
        self._db.commit()

        updated_row = self._db.execute(
            "SELECT id, version, content, created_at, is_active, created_by "
            "FROM prompt_versions WHERE version = ?",
            (version,),
        ).fetchone()
        return self._row_to_prompt_version(updated_row)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_prompt_version(row: sqlite3.Row) -> PromptVersion:
        d = dict(row)
        return PromptVersion(
            id=d["id"],
            version=d["version"],
            content=d["content"],
            created_at=d["created_at"],
            is_active=bool(d["is_active"]),
            created_by=d.get("created_by"),
        )
