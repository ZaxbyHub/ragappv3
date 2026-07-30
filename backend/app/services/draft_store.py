"""SQLite persistence for Draft Room (issue #435, ``specs/draft-room/SPEC.md``).

``DraftStore`` owns *all* Draft Room CRUD, atomic job claims, centralized status
transitions, revision numbering, and audit-event records. Routes and the durable
job processor go through it; no other module writes these tables.

Invariants enforced here that SQLite cannot express (SPEC sections 5.7, 9.1, 10.3):

* Every read/write is scoped by both ``draft_id`` **and** ``created_by``. A child
  row (input, job, revision, event) is only ever reachable through its owning
  draft, so a child ID is never sufficient authorization on its own.
* ``draft_jobs.vault_id``/``created_by`` equal the owning draft's values, and
  ``input_id`` is non-null only for ``parse_input`` jobs belonging to that draft.
* ``draft_revisions.job_id`` is null for manual revisions and otherwise names a
  job on the same draft; ``parent_revision_id`` belongs to the same draft.
* A draft's owner and vault are immutable after creation.
* No caller may write an arbitrary status string — every status change goes
  through the transition tables below and is rejected otherwise.
* Revision-number allocation and current-revision replacement happen inside a
  single ``BEGIN IMMEDIATE`` transaction.

The store is synchronous (SQLite is sync). Async callers wrap it in
``asyncio.to_thread`` per repository convention, and the durable processor never
holds a connection across an ``await``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────────────


class DraftStoreError(Exception):
    """Base class for Draft Room store failures."""

    code = "internal_error"


class DraftNotFoundError(DraftStoreError):
    """The draft (or a child row reached through it) does not exist for this owner.

    Callers translate this to ``404``: a draft owned by another user and a draft
    that never existed are deliberately indistinguishable.
    """

    code = "not_found"


class DraftConflictError(DraftStoreError):
    """A concurrency or state conflict — stale ``lock_version``, active job,
    duplicate input, or an invalid state transition. Callers translate to ``409``."""

    code = "conflict"


class DraftLimitExceededError(DraftStoreError):
    """A configured project limit was exceeded. Callers translate to ``413``."""

    code = "limit_exceeded"


class DraftValidationError(DraftStoreError):
    """A value violated a store-layer invariant. Callers translate to ``422``."""

    code = "validation_failed"


class InvalidTransitionError(DraftConflictError):
    """A status change was requested that the state machine forbids."""

    code = "invalid_transition"


# ── Status vocabularies and transitions (SPEC section 10.3) ──────────────────

DRAFT_STATUSES: frozenset[str] = frozenset(
    {
        "draft",
        "queued",
        "running",
        "needs_review",
        "ready",
        "failed",
        "cancelled",
        "archived",
    }
)
JOB_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed", "cancelled"}
)
INPUT_STATUSES: frozenset[str] = frozenset(
    {"pending", "parsing", "ready", "failed", "cancelled"}
)

DRAFT_MODES: frozenset[str] = frozenset({"rewrite", "compose"})
DRAFT_TIERS: frozenset[str] = frozenset({"standard", "high_stakes", "sensitive"})
INPUT_ROLES: frozenset[str] = frozenset(
    {"manuscript", "reference", "style", "background", "challenge"}
)
INPUT_AUTHORITIES: frozenset[str] = frozenset(
    {"primary", "official", "secondary", "user_asserted", "unknown"}
)
JOB_TYPES: frozenset[str] = frozenset({"parse_input", "compile"})
REVISION_SOURCES: frozenset[str] = frozenset({"pipeline", "manual"})

# Transitions reachable through ordinary operation.
_DRAFT_TRANSITIONS: dict[str, frozenset[str]] = {
    # 'draft' -> 'needs_review' is the manual-authoring edge. SPEC section 10.3
    # lists the compile lifecycle (draft -> queued -> running -> needs_review),
    # but section 3.3 states that saving a manual revision sets the project to
    # needs_review, and this release ships manual revisions with no compile
    # path at all. Without this edge the very first manual save on a new draft
    # is rejected and the feature is unreachable.
    "draft": frozenset({"queued", "needs_review", "archived"}),
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"needs_review", "failed", "cancelled"}),
    "needs_review": frozenset({"queued", "ready", "archived"}),
    "ready": frozenset({"needs_review", "archived"}),
    "failed": frozenset({"queued", "archived"}),
    "cancelled": frozenset({"queued", "archived"}),
    "archived": frozenset({"draft", "needs_review"}),
}
_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_INPUT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"parsing", "failed", "cancelled"}),
    "parsing": frozenset({"ready", "failed", "cancelled"}),
    "ready": frozenset(),
    "failed": frozenset({"pending"}),
    "cancelled": frozenset({"pending"}),
}

# Transitions permitted *only* to startup orphan recovery (SPEC section 10.3).
# They are separated so ordinary code paths cannot reach them by accident.
_DRAFT_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"queued"}),
}
_JOB_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"pending"}),
}
_INPUT_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "parsing": frozenset({"pending"}),
}

# Statuses that mean a job is still consuming resources.
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("pending", "running")

_MAX_ERROR_MESSAGE_LEN = 2000
_MAX_TITLE_LEN = 300


def _check_transition(
    kind: str,
    current: str,
    target: str,
    table: dict[str, frozenset[str]],
    recovery_table: dict[str, frozenset[str]],
    *,
    allow_recovery: bool = False,
) -> None:
    """Reject any status change the state machine does not allow.

    Args:
        kind: Entity name used in the error message (``draft``/``job``/``input``).
        current: The row's present status.
        target: The requested status.
        table: Ordinary transition map.
        recovery_table: Transitions reserved for startup orphan recovery.
        allow_recovery: True only when called from startup recovery.

    Raises:
        InvalidTransitionError: If the transition is not permitted.
    """
    if target == current:
        return
    allowed = set(table.get(current, frozenset()))
    if allow_recovery:
        allowed |= set(recovery_table.get(current, frozenset()))
    if target not in allowed:
        raise InvalidTransitionError(
            f"{kind} cannot move from '{current}' to '{target}'"
        )


def sha256_text(text: str) -> str:
    """SHA-256 of ``text`` encoded as UTF-8, as lowercase hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate(value: Optional[str], limit: int) -> Optional[str]:
    """Bound a stored diagnostic string without raising on ``None``."""
    if value is None:
        return None
    return value[:limit]


# ── Records ──────────────────────────────────────────────────────────────────


@dataclass
class DraftRecord:
    """A Draft Room project row."""

    id: int
    vault_id: int
    created_by: int
    title: str
    mode: str
    status: str
    tier: str
    brief_json: str
    lock_version: int
    ready_revision_id: Optional[int]
    ready_by: Optional[int]
    ready_at: Optional[str]
    archived_at: Optional[str]
    created_at: str
    updated_at: str


@dataclass
class DraftInputRecord:
    """An uploaded project input. ``storage_relpath`` is never exposed over HTTP."""

    id: int
    draft_id: int
    role: str
    authority: str
    as_of_date: Optional[str]
    original_name: str
    stored_name: str
    extension: str
    media_type: Optional[str]
    size_bytes: int
    content_sha256: str
    storage_relpath: str
    parsed_text_sha256: Optional[str]
    parsed_char_count: Optional[int]
    parse_status: str
    parse_error: Optional[str]
    locked_spans_json: str
    created_at: str
    updated_at: str


@dataclass
class DraftJobRecord:
    """A durable Draft Room job."""

    id: int
    draft_id: int
    vault_id: int
    created_by: int
    job_type: str
    input_id: Optional[int]
    parent_job_id: Optional[int]
    attempt_no: int
    idempotency_key: Optional[str]
    status: str
    active_stage: Optional[str]
    start_stage: Optional[str]
    retry_count: int
    model_call_count: int
    max_model_calls: int
    timeout_seconds: int
    progress_percent: float
    cancel_requested_at: Optional[str]
    heartbeat_at: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]


@dataclass
class DraftRevisionRecord:
    """An immutable revision row. ``content_md`` is loaded only on detail reads."""

    id: int
    draft_id: int
    parent_revision_id: Optional[int]
    job_id: Optional[int]
    revision_no: int
    source: str
    content_sha256: str
    fact_status: str
    is_current: int
    created_by: Optional[int]
    created_at: str
    content_md: Optional[str] = None
    sections_json: str = "[]"
    citations_json: str = "[]"
    qa_summary_json: str = "{}"


_DRAFT_COLUMNS = (
    "id, vault_id, created_by, title, mode, status, tier, brief_json, "
    "lock_version, ready_revision_id, ready_by, ready_at, archived_at, "
    "created_at, updated_at"
)
_INPUT_COLUMNS = (
    "id, draft_id, role, authority, as_of_date, original_name, stored_name, "
    "extension, media_type, size_bytes, content_sha256, storage_relpath, "
    "parsed_text_sha256, parsed_char_count, parse_status, parse_error, "
    "locked_spans_json, created_at, updated_at"
)
_JOB_COLUMNS = (
    "id, draft_id, vault_id, created_by, job_type, input_id, parent_job_id, "
    "attempt_no, idempotency_key, status, active_stage, start_stage, "
    "retry_count, model_call_count, max_model_calls, timeout_seconds, "
    "progress_percent, cancel_requested_at, heartbeat_at, error_code, "
    "error_message, created_at, started_at, completed_at"
)
_REVISION_SUMMARY_COLUMNS = (
    "id, draft_id, parent_revision_id, job_id, revision_no, source, "
    "content_sha256, fact_status, is_current, created_by, created_at"
)


def _row_to_draft(row: sqlite3.Row) -> DraftRecord:
    return DraftRecord(*row)


def _row_to_input(row: sqlite3.Row) -> DraftInputRecord:
    return DraftInputRecord(*row)


def _row_to_job(row: sqlite3.Row) -> DraftJobRecord:
    return DraftJobRecord(*row)


def _row_to_revision_summary(row: sqlite3.Row) -> DraftRevisionRecord:
    return DraftRevisionRecord(*row)


class DraftStore:
    """All Draft Room SQLite access.

    Args:
        db: An open SQLite connection with ``PRAGMA foreign_keys = ON``. The
            caller owns its lifetime; the store never closes it.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # ── transaction helper ───────────────────────────────────────────────

    def _begin_immediate(self) -> None:
        """Take the write lock, clearing any dangling implicit transaction first.

        A prior best-effort statement on this pooled connection may have left an
        implicit transaction open, which would make ``BEGIN IMMEDIATE`` fail with
        "cannot start a transaction within a transaction".
        """
        if self._db.in_transaction:
            self._db.rollback()
        self._db.execute("BEGIN IMMEDIATE")

    # ── drafts ───────────────────────────────────────────────────────────

    def create_draft(
        self,
        *,
        vault_id: int,
        created_by: int,
        title: str,
        mode: str,
        tier: str,
        brief_json: str,
    ) -> DraftRecord:
        """Create an owner-private project and record a ``draft_created`` event.

        Owner and vault are fixed here and can never be changed afterwards —
        there is deliberately no store method that updates either column.
        """
        title = (title or "").strip()
        if not title:
            raise DraftValidationError("title must not be empty")
        if len(title) > _MAX_TITLE_LEN:
            raise DraftValidationError(
                f"title must be at most {_MAX_TITLE_LEN} characters"
            )
        if mode not in DRAFT_MODES:
            raise DraftValidationError(f"unknown draft mode: {mode!r}")
        if tier not in DRAFT_TIERS:
            raise DraftValidationError(f"unknown draft tier: {tier!r}")

        self._begin_immediate()
        try:
            cur = self._db.execute(
                "INSERT INTO drafts (vault_id, created_by, title, mode, tier, brief_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (vault_id, created_by, title, mode, tier, brief_json),
            )
            draft_id = int(cur.lastrowid)
            self._insert_event(
                draft_id=draft_id,
                event_type="draft_created",
                actor_user_id=created_by,
                payload={"mode": mode, "tier": tier, "vault_id": vault_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, created_by)

    def get_draft(self, draft_id: int, owner_id: int) -> DraftRecord:
        """Load a draft by ``(draft_id, owner_id)``.

        Raises:
            DraftNotFoundError: If it is absent or owned by someone else. The
                two cases are intentionally indistinguishable so draft existence
                is not disclosed across owners.
        """
        row = self._db.execute(
            "SELECT id, vault_id, created_by, title, mode, status, tier, brief_json, "
            "lock_version, ready_revision_id, ready_by, ready_at, archived_at, "
            "created_at, updated_at FROM drafts WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("draft not found")
        return _row_to_draft(row)

    def list_drafts(
        self,
        *,
        owner_id: int,
        vault_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[DraftRecord], int]:
        """Page through the caller's own drafts, newest first.

        Rows whose vault access has since been revoked are still returned so the
        owner can see and delete their private data (SPEC section 9.1); the route
        annotates each row's current ``vault_access``.

        Returns:
            ``(rows, total)`` ordered by ``updated_at DESC, id DESC`` — the ID
            tie-break keeps paging stable when timestamps collide.
        """
        if status is not None and status not in DRAFT_STATUSES:
            raise DraftValidationError(f"unknown draft status: {status!r}")
        where = ["created_by = ?"]
        params: list[Any] = [owner_id]
        if vault_id is not None:
            where.append("vault_id = ?")
            params.append(vault_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where)

        # clause is built only from literal fragments; all values are bound parameters
        total = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM drafts WHERE {clause}", params  # nosec B608
            ).fetchone()[0]
        )
        # clause is built only from literal fragments; all values are bound parameters
        rows = self._db.execute(
            f"SELECT {_DRAFT_COLUMNS} FROM drafts WHERE {clause} "  # nosec B608
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, per_page, max(page - 1, 0) * per_page),
        ).fetchall()
        return [_row_to_draft(r) for r in rows], total

    def update_draft(
        self,
        *,
        draft_id: int,
        owner_id: int,
        lock_version: int,
        title: Optional[str] = None,
        brief_json: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> DraftRecord:
        """Update title/brief/tier under an optimistic lock.

        Raises:
            DraftConflictError: If ``lock_version`` is stale or a compile job is
                active (an in-flight job snapshotted the brief it is running on).
        """
        if tier is not None and tier not in DRAFT_TIERS:
            raise DraftValidationError(f"unknown draft tier: {tier!r}")
        if title is not None:
            title = title.strip()
            if not title:
                raise DraftValidationError("title must not be empty")
            if len(title) > _MAX_TITLE_LEN:
                raise DraftValidationError(
                    f"title must be at most {_MAX_TITLE_LEN} characters"
                )

        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            if draft.status == "archived":
                raise InvalidTransitionError("archived draft cannot be edited")
            self._assert_no_active_compile(draft_id)

            self._db.execute(
                "UPDATE drafts SET title = COALESCE(?, title), "
                "brief_json = COALESCE(?, brief_json), tier = COALESCE(?, tier), "
                "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND created_by = ?",
                (title, brief_json, tier, draft_id, owner_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, owner_id)

    def archive_draft(
        self, *, draft_id: int, owner_id: int, lock_version: int
    ) -> DraftRecord:
        """Archive a project. Input bytes are retained (SPEC section 6.1)."""
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            _check_transition(
                "draft", draft.status, "archived",
                _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
            )
            self._assert_no_active_job(draft_id)
            self._db.execute(
                "UPDATE drafts SET status = 'archived', archived_at = CURRENT_TIMESTAMP, "
                "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND created_by = ?",
                (draft_id, owner_id),
            )
            self._insert_event(
                draft_id=draft_id,
                event_type="draft_archived",
                actor_user_id=owner_id,
                payload={},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, owner_id)

    def restore_draft(
        self, *, draft_id: int, owner_id: int, lock_version: int
    ) -> DraftRecord:
        """Unarchive a project.

        Restores to ``needs_review`` when a current revision exists, otherwise to
        ``draft``. It never restores directly to ``ready`` — only a human action
        on a specific revision may set that.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            if draft.status != "archived":
                raise InvalidTransitionError("draft is not archived")
            has_current = (
                self._db.execute(
                    "SELECT 1 FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
                    (draft_id,),
                ).fetchone()
                is not None
            )
            target = "needs_review" if has_current else "draft"
            _check_transition(
                "draft", draft.status, target,
                _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE drafts SET status = ?, archived_at = NULL, "
                "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND created_by = ?",
                (target, draft_id, owner_id),
            )
            self._insert_event(
                draft_id=draft_id,
                event_type="draft_restored",
                actor_user_id=owner_id,
                payload={"status": target},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, owner_id)

    def _locked_draft(
        self, draft_id: int, owner_id: int, lock_version: Optional[int]
    ) -> DraftRecord:
        """Read a draft inside an open write transaction and check its lock."""
        row = self._db.execute(
            "SELECT id, vault_id, created_by, title, mode, status, tier, brief_json, "
            "lock_version, ready_revision_id, ready_by, ready_at, archived_at, "
            "created_at, updated_at FROM drafts WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("draft not found")
        draft = _row_to_draft(row)
        if lock_version is not None and draft.lock_version != lock_version:
            raise DraftConflictError(
                "draft was modified by another request; reload and retry"
            )
        return draft

    def _assert_no_active_compile(self, draft_id: int) -> None:
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE draft_id = ? AND job_type = 'compile' "
            "AND status IN ('pending','running') LIMIT 1",
            (draft_id,),
        ).fetchone()
        if row is not None:
            raise DraftConflictError("a compile job is active for this draft")

    def _assert_no_active_job(self, draft_id: int) -> None:
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE draft_id = ? "
            "AND status IN ('pending','running') LIMIT 1",
            (draft_id,),
        ).fetchone()
        if row is not None:
            raise DraftConflictError("a job is active for this draft")

    def count_active_jobs(self, draft_id: int) -> int:
        """Number of pending/running jobs on a draft (delete/archive guards)."""
        return int(
            self._db.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND status IN ('pending','running')",
                (draft_id,),
            ).fetchone()[0]
        )

    # ── events ───────────────────────────────────────────────────────────

    def _insert_event(
        self,
        *,
        draft_id: int,
        event_type: str,
        actor_user_id: Optional[int] = None,
        job_id: Optional[int] = None,
        revision_id: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append an audit row inside the caller's open transaction.

        ``payload`` is a small JSON object of IDs and reason codes. Never pass
        document content, prompts, paths, or raw exception text — ``draft_events``
        is project history, not a log sink.
        """
        self._db.execute(
            "INSERT INTO draft_events (draft_id, job_id, revision_id, actor_user_id, "
            "event_type, event_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                job_id,
                revision_id,
                actor_user_id,
                event_type,
                json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    def record_event(
        self,
        *,
        draft_id: int,
        owner_id: int,
        event_type: str,
        actor_user_id: Optional[int] = None,
        job_id: Optional[int] = None,
        revision_id: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append and commit a standalone audit event for an owned draft."""
        self.get_draft(draft_id, owner_id)
        self._insert_event(
            draft_id=draft_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            job_id=job_id,
            revision_id=revision_id,
            payload=payload,
        )
        self._db.commit()

    # ── inputs ───────────────────────────────────────────────────────────

    def reserve_input(
        self,
        *,
        draft_id: int,
        owner_id: int,
        role: str,
        authority: str,
        as_of_date: Optional[str],
        original_name: str,
        stored_name: str,
        extension: str,
        media_type: Optional[str],
        size_bytes: int,
        content_sha256: str,
        max_inputs: int,
        max_total_input_bytes: int,
    ) -> DraftInputRecord:
        """Atomically reserve an input row and allocate its storage path.

        Runs in one ``BEGIN IMMEDIATE`` transaction that re-checks ownership,
        the per-project input count and total-bytes limits, and duplicate content
        hash *after* taking the write lock — so two concurrent uploads cannot both
        pass a check-then-act race.

        The relative path is derived from the row's own ``lastrowid`` and is
        therefore unique by construction; the client filename never appears in it.

        Raises:
            DraftNotFoundError: The draft is absent or owned by someone else.
            DraftConflictError: Identical content already exists in this draft.
                The exception carries ``existing_input_id``.
            DraftLimitExceededError: An input-count or total-bytes limit was hit.
        """
        if role not in INPUT_ROLES:
            raise DraftValidationError(f"unknown input role: {role!r}")
        if authority not in INPUT_AUTHORITIES:
            raise DraftValidationError(f"unknown input authority: {authority!r}")
        if size_bytes < 0:
            raise DraftValidationError("size_bytes must not be negative")

        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, None)
            if draft.status == "archived":
                raise InvalidTransitionError(
                    "archived draft cannot accept new inputs"
                )
            self._assert_no_active_compile(draft_id)

            dup = self._db.execute(
                "SELECT id FROM draft_inputs WHERE draft_id = ? AND content_sha256 = ?",
                (draft_id, content_sha256),
            ).fetchone()
            if dup is not None:
                err = DraftConflictError("input content already exists in this draft")
                err.code = "duplicate_input"
                err.existing_input_id = int(dup[0])  # type: ignore[attr-defined]
                raise err

            count_row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM draft_inputs "
                "WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            current_count = int(count_row[0])
            current_bytes = int(count_row[1])
            if current_count >= max_inputs:
                raise DraftLimitExceededError(
                    f"draft already holds the maximum of {max_inputs} inputs"
                )
            if current_bytes + size_bytes > max_total_input_bytes:
                raise DraftLimitExceededError(
                    "draft would exceed its total input size limit"
                )

            # Insert with a temporary unique placeholder: storage_relpath is NOT
            # NULL and UNIQUE, and the real path needs the row id we do not have
            # until after the insert.
            placeholder = f"pending:{draft_id}:{content_sha256}"
            cur = self._db.execute(
                "INSERT INTO draft_inputs (draft_id, role, authority, as_of_date, "
                "original_name, stored_name, extension, media_type, size_bytes, "
                "content_sha256, storage_relpath) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id,
                    role,
                    authority,
                    as_of_date,
                    original_name,
                    stored_name,
                    extension,
                    media_type,
                    size_bytes,
                    content_sha256,
                    placeholder,
                ),
            )
            input_id = int(cur.lastrowid)
            relpath = build_input_relpath(
                owner_id=owner_id,
                draft_id=draft_id,
                input_id=input_id,
                stored_name=stored_name,
            )
            self._db.execute(
                "UPDATE draft_inputs SET storage_relpath = ? WHERE id = ?",
                (relpath, input_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)

    def get_input(
        self, *, draft_id: int, owner_id: int, input_id: int
    ) -> DraftInputRecord:
        """Load one input, constrained through its owning draft."""
        row = self._db.execute(
            "SELECT id, draft_id, role, authority, as_of_date, original_name, "
            "stored_name, extension, media_type, size_bytes, content_sha256, "
            "storage_relpath, parsed_text_sha256, parsed_char_count, parse_status, "
            "parse_error, locked_spans_json, created_at, updated_at "
            "FROM draft_inputs i "
            "WHERE i.id = ? AND i.draft_id = ? AND EXISTS ("
            "  SELECT 1 FROM drafts d WHERE d.id = i.draft_id AND d.created_by = ?)",
            (input_id, draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("input not found")
        return _row_to_input(row)

    def list_inputs(self, *, draft_id: int, owner_id: int) -> list[DraftInputRecord]:
        """All inputs for an owned draft, oldest first."""
        self.get_draft(draft_id, owner_id)
        rows = self._db.execute(
            "SELECT id, draft_id, role, authority, as_of_date, original_name, "
            "stored_name, extension, media_type, size_bytes, content_sha256, "
            "storage_relpath, parsed_text_sha256, parsed_char_count, parse_status, "
            "parse_error, locked_spans_json, created_at, updated_at "
            "FROM draft_inputs "
            "WHERE draft_id = ? ORDER BY created_at ASC, id ASC",
            (draft_id,),
        ).fetchall()
        return [_row_to_input(r) for r in rows]

    def get_input_parsed_text(
        self, *, draft_id: int, owner_id: int, input_id: int
    ) -> Optional[str]:
        """Parsed text for one input.

        Kept off :class:`DraftInputRecord` on purpose: parsed text must never
        appear in list or detail responses, only through the dedicated content
        endpoint (SPEC section 8.2).
        """
        self.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)
        row = self._db.execute(
            "SELECT parsed_text FROM draft_inputs WHERE id = ? AND draft_id = ?",
            (input_id, draft_id),
        ).fetchone()
        return None if row is None else row[0]

    def update_input_metadata(
        self,
        *,
        draft_id: int,
        owner_id: int,
        input_id: int,
        role: Optional[str] = None,
        authority: Optional[str] = None,
        as_of_date: Optional[str] = None,
        clear_as_of_date: bool = False,
        locked_spans_json: Optional[str] = None,
    ) -> DraftInputRecord:
        """Update an input's role/authority/as-of date/locked spans.

        Not permitted while a compile job is active: a running job snapshotted
        these values and must not observe them change underneath it.
        """
        if role is not None and role not in INPUT_ROLES:
            raise DraftValidationError(f"unknown input role: {role!r}")
        if authority is not None and authority not in INPUT_AUTHORITIES:
            raise DraftValidationError(f"unknown input authority: {authority!r}")

        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            self._assert_no_active_compile(draft_id)
            existing = self._db.execute(
                "SELECT id FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            ).fetchone()
            if existing is None:
                raise DraftNotFoundError("input not found")

            if clear_as_of_date:
                self._db.execute(
                    "UPDATE draft_inputs SET as_of_date = NULL WHERE id = ?",
                    (input_id,),
                )
            self._db.execute(
                "UPDATE draft_inputs SET role = COALESCE(?, role), "
                "authority = COALESCE(?, authority), "
                "as_of_date = COALESCE(?, as_of_date), "
                "locked_spans_json = COALESCE(?, locked_spans_json), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND draft_id = ?",
                (role, authority, as_of_date, locked_spans_json, input_id, draft_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)

    def set_input_parse_status(
        self,
        *,
        input_id: int,
        target: str,
        parsed_text: Optional[str] = None,
        parsed_text_sha256: Optional[str] = None,
        parsed_char_count: Optional[int] = None,
        parse_error: Optional[str] = None,
        allow_recovery: bool = False,
    ) -> None:
        """Move an input through its parse state machine and commit.

        Args:
            target: The new ``parse_status``; validated against the transition
                table so no caller can write an arbitrary string.
            allow_recovery: Only startup orphan recovery may pass True, which
                additionally permits ``parsing -> pending``.

        Raises:
            DraftNotFoundError: The input row is gone.
            InvalidTransitionError: The transition is not allowed.
        """
        if target not in INPUT_STATUSES:
            raise DraftValidationError(f"unknown input parse status: {target!r}")
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT parse_status FROM draft_inputs WHERE id = ?", (input_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("input not found")
            _check_transition(
                "input", row[0], target,
                _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS,
                allow_recovery=allow_recovery,
            )
            self._db.execute(
                "UPDATE draft_inputs SET parse_status = ?, "
                "parsed_text = COALESCE(?, parsed_text), "
                "parsed_text_sha256 = COALESCE(?, parsed_text_sha256), "
                "parsed_char_count = COALESCE(?, parsed_char_count), "
                "parse_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    target,
                    parsed_text,
                    parsed_text_sha256,
                    parsed_char_count,
                    _truncate(parse_error, _MAX_ERROR_MESSAGE_LEN),
                    input_id,
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def total_parsed_chars(self, draft_id: int, *, excluding_input_id: int) -> int:
        """Sum of parsed characters across a draft's ready inputs.

        ``excluding_input_id`` omits the input currently being parsed so its own
        pending contribution is not double counted by the caller.
        """
        row = self._db.execute(
            "SELECT COALESCE(SUM(parsed_char_count), 0) FROM draft_inputs "
            "WHERE draft_id = ? AND id != ? AND parse_status = 'ready'",
            (draft_id, excluding_input_id),
        ).fetchone()
        return int(row[0])

    def delete_input_row(
        self, *, draft_id: int, owner_id: int, input_id: int
    ) -> None:
        """Delete an input row (phase 2 of the tombstone flow) and audit it.

        The file is renamed into ``.trash`` *before* this runs and is restored if
        this transaction fails, so bytes and rows can never disagree.
        """
        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            cur = self._db.execute(
                "DELETE FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            )
            if cur.rowcount == 0:
                raise DraftNotFoundError("input not found")
            self._insert_event(
                draft_id=draft_id,
                event_type="input_deleted",
                actor_user_id=owner_id,
                payload={"input_id": input_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def input_is_in_use(self, *, draft_id: int, input_id: int) -> bool:
        """True when a completed job derived durable artifacts from this input.

        A used input cannot be deleted piecemeal (``409 input_in_use``): immutable
        revisions contain derivatives, and pretending otherwise would be dishonest.
        Whole-draft deletion is the comprehensive purge.
        """
        row = self._db.execute(
            "SELECT 1 FROM draft_jobs WHERE draft_id = ? AND input_id = ? "
            "AND job_type = 'compile' AND status = 'completed' LIMIT 1",
            (draft_id, input_id),
        ).fetchone()
        if row is not None:
            return True
        row = self._db.execute(
            "SELECT 1 FROM draft_revisions r JOIN draft_jobs j ON j.id = r.job_id "
            "WHERE r.draft_id = ? AND j.input_id = ? LIMIT 1",
            (draft_id, input_id),
        ).fetchone()
        return row is not None

    # ── revisions ────────────────────────────────────────────────────────

    def create_manual_revision(
        self,
        *,
        draft_id: int,
        owner_id: int,
        lock_version: int,
        base_revision_id: Optional[int],
        content_md: str,
    ) -> DraftRevisionRecord:
        """Save an immutable manual Markdown revision.

        The whole allocation runs in one ``BEGIN IMMEDIATE`` transaction, exactly
        as SPEC section 5.3 requires: clear the previous current flag, allocate
        ``MAX(revision_no) + 1``, insert the immutable row, mark it current, move
        the project to ``needs_review``, clear Ready state, and bump the lock.

        ``base_revision_id`` must name the draft's current revision, or be null
        when the draft has none — this is what makes a concurrent edit fail loudly
        instead of silently forking history.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            if draft.status == "archived":
                raise InvalidTransitionError(
                    "archived draft cannot accept new revisions"
                )
            self._assert_no_active_compile(draft_id)

            current = self._db.execute(
                "SELECT id FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
                (draft_id,),
            ).fetchone()
            current_id = None if current is None else int(current[0])
            if base_revision_id != current_id:
                raise DraftConflictError(
                    "base_revision_id does not match the draft's current revision"
                )

            next_no = int(
                self._db.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM draft_revisions "
                    "WHERE draft_id = ?",
                    (draft_id,),
                ).fetchone()[0]
            )
            # Clear the old current flag first: the partial unique index allows
            # only one is_current=1 row per draft at any instant.
            if current_id is not None:
                self._db.execute(
                    "UPDATE draft_revisions SET is_current = 0 WHERE id = ?",
                    (current_id,),
                )
            cur = self._db.execute(
                "INSERT INTO draft_revisions (draft_id, parent_revision_id, job_id, "
                "revision_no, source, content_md, content_sha256, fact_status, "
                "is_current, created_by) "
                "VALUES (?, ?, NULL, ?, 'manual', ?, ?, 'not_run', 1, ?)",
                (
                    draft_id,
                    current_id,
                    next_no,
                    content_md,
                    sha256_text(content_md),
                    owner_id,
                ),
            )
            revision_id = int(cur.lastrowid)

            # A manual edit is a semantic change: any prior Ready approval and
            # factual result no longer describe the current text.
            target_status = "needs_review"
            if draft.status not in ("needs_review",):
                _check_transition(
                    "draft", draft.status, target_status,
                    _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
                )
            self._db.execute(
                "UPDATE drafts SET status = ?, ready_revision_id = NULL, "
                "ready_by = NULL, ready_at = NULL, lock_version = lock_version + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND created_by = ?",
                (target_status, draft_id, owner_id),
            )
            self._insert_event(
                draft_id=draft_id,
                event_type="revision_created",
                actor_user_id=owner_id,
                revision_id=revision_id,
                payload={"source": "manual", "revision_no": next_no},
            )
            if draft.ready_revision_id is not None:
                self._insert_event(
                    draft_id=draft_id,
                    event_type="ready_invalidated",
                    actor_user_id=owner_id,
                    revision_id=revision_id,
                    payload={"reason": "manual_revision"},
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_revision(
            draft_id=draft_id, owner_id=owner_id, revision_id=revision_id
        )

    def get_revision(
        self,
        *,
        draft_id: int,
        owner_id: int,
        revision_id: int,
        include_content: bool = True,
    ) -> DraftRevisionRecord:
        """Load one immutable revision, constrained through its owning draft."""
        if include_content:
            query = (
                "SELECT id, draft_id, parent_revision_id, job_id, revision_no, "
                "source, content_sha256, fact_status, is_current, created_by, "
                "created_at, content_md, sections_json, citations_json, "
                "qa_summary_json FROM draft_revisions r "
                "WHERE r.id = ? AND r.draft_id = ? AND EXISTS ("
                "  SELECT 1 FROM drafts d WHERE d.id = r.draft_id AND d.created_by = ?)"
            )
        else:
            query = (
                "SELECT id, draft_id, parent_revision_id, job_id, revision_no, "
                "source, content_sha256, fact_status, is_current, created_by, "
                "created_at FROM draft_revisions r "
                "WHERE r.id = ? AND r.draft_id = ? AND EXISTS ("
                "  SELECT 1 FROM drafts d WHERE d.id = r.draft_id AND d.created_by = ?)"
            )
        row = self._db.execute(
            query,
            (revision_id, draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("revision not found")
        return _row_to_revision_summary(row)

    def get_current_revision(
        self, *, draft_id: int, owner_id: int
    ) -> Optional[DraftRevisionRecord]:
        """The draft's current revision summary, or None when it has none."""
        self.get_draft(draft_id, owner_id)
        row = self._db.execute(
            "SELECT id, draft_id, parent_revision_id, job_id, revision_no, source, "
            "content_sha256, fact_status, is_current, created_by, created_at "
            "FROM draft_revisions "
            "WHERE draft_id = ? AND is_current = 1",
            (draft_id,),
        ).fetchone()
        return None if row is None else _row_to_revision_summary(row)

    def list_revisions(
        self, *, draft_id: int, owner_id: int, page: int = 1, per_page: int = 50
    ) -> tuple[list[DraftRevisionRecord], int]:
        """Page through revision metadata, newest first. Never returns Markdown."""
        self.get_draft(draft_id, owner_id)
        total = int(
            self._db.execute(
                "SELECT COUNT(*) FROM draft_revisions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()[0]
        )
        rows = self._db.execute(
            "SELECT id, draft_id, parent_revision_id, job_id, revision_no, source, "
            "content_sha256, fact_status, is_current, created_by, created_at "
            "FROM draft_revisions "
            "WHERE draft_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (draft_id, per_page, max(page - 1, 0) * per_page),
        ).fetchall()
        return [_row_to_revision_summary(r) for r in rows], total

    def count_revisions(self, draft_id: int) -> int:
        """Number of revisions on a draft."""
        return int(
            self._db.execute(
                "SELECT COUNT(*) FROM draft_revisions WHERE draft_id = ?", (draft_id,)
            ).fetchone()[0]
        )

    # ── jobs ─────────────────────────────────────────────────────────────

    def enqueue_parse_job(
        self, *, draft_id: int, owner_id: int, input_id: int, timeout_seconds: int
    ) -> DraftJobRecord:
        """Enqueue the single durable ``parse_input`` job for an input.

        ``max_model_calls`` is fixed at 0: parsing never calls a model. The
        partial unique index guarantees at most one active parse job per input;
        hitting it surfaces as a conflict rather than a 500.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, None)
            row = self._db.execute(
                "SELECT id FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("input not found")
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                    "input_id, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'parse_input', ?, 0, ?)",
                    (draft_id, draft.vault_id, draft.created_by, input_id, timeout_seconds),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    "a parse job is already active for this input"
                ) from exc
            job_id = int(cur.lastrowid)
            self._insert_event(
                draft_id=draft_id,
                event_type="input_uploaded",
                actor_user_id=owner_id,
                job_id=job_id,
                payload={"input_id": input_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)

    def get_job(self, *, draft_id: int, owner_id: int, job_id: int) -> DraftJobRecord:
        """Load one job, constrained through its owning draft."""
        row = self._db.execute(
            "SELECT id, draft_id, vault_id, created_by, job_type, input_id, "
            "parent_job_id, attempt_no, idempotency_key, status, active_stage, "
            "start_stage, retry_count, model_call_count, max_model_calls, "
            "timeout_seconds, progress_percent, cancel_requested_at, heartbeat_at, "
            "error_code, error_message, created_at, started_at, completed_at "
            "FROM draft_jobs j "
            "WHERE j.id = ? AND j.draft_id = ? AND EXISTS ("
            "  SELECT 1 FROM drafts d WHERE d.id = j.draft_id AND d.created_by = ?)",
            (job_id, draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("job not found")
        return _row_to_job(row)

    def list_jobs(
        self,
        *,
        draft_id: int,
        owner_id: int,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[DraftJobRecord], int]:
        """Page through a draft's job history, newest first."""
        self.get_draft(draft_id, owner_id)
        if job_type is not None and job_type not in JOB_TYPES:
            raise DraftValidationError(f"unknown job type: {job_type!r}")
        if status is not None and status not in JOB_STATUSES:
            raise DraftValidationError(f"unknown job status: {status!r}")
        where = ["draft_id = ?"]
        params: list[Any] = [draft_id]
        if job_type is not None:
            where.append("job_type = ?")
            params.append(job_type)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where)
        # clause is built only from literal fragments; all values are bound parameters
        total = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM draft_jobs WHERE {clause}", params  # nosec B608
            ).fetchone()[0]
        )
        # clause is built only from literal fragments; all values are bound parameters
        rows = self._db.execute(
            f"SELECT {_JOB_COLUMNS} FROM draft_jobs WHERE {clause} "  # nosec B608
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, per_page, max(page - 1, 0) * per_page),
        ).fetchall()
        return [_row_to_job(r) for r in rows], total

    def get_active_parse_job_id(self, input_id: int) -> Optional[int]:
        """ID of the input's active parse job, if any."""
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE input_id = ? AND job_type = 'parse_input' "
            "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1",
            (input_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def get_last_parse_job_id(self, input_id: int) -> Optional[int]:
        """ID of the input's most recent parse job in any status."""
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE input_id = ? AND job_type = 'parse_input' "
            "ORDER BY id DESC LIMIT 1",
            (input_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def claim_next_parse_job(self) -> Optional[DraftJobRecord]:
        """Atomically claim one pending ``parse_input`` job.

        Takes the write lock with ``BEGIN IMMEDIATE`` and only flips
        ``pending -> running`` while the status is *still* pending, so a second
        processor racing for the same row claims nothing rather than double-running
        it. Returns None when the queue is empty.
        """
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT id FROM draft_jobs WHERE status = 'pending' "
                "AND job_type = 'parse_input' ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                self._db.rollback()
                return None
            job_id = int(row[0])
            cur = self._db.execute(
                "UPDATE draft_jobs SET status = 'running', "
                "started_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'pending'",
                (job_id,),
            )
            if cur.rowcount == 0:
                self._db.rollback()
                return None
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        row = self._db.execute(
            "SELECT id, draft_id, vault_id, created_by, job_type, input_id, "
            "parent_job_id, attempt_no, idempotency_key, status, active_stage, "
            "start_stage, retry_count, model_call_count, max_model_calls, "
            "timeout_seconds, progress_percent, cancel_requested_at, heartbeat_at, "
            "error_code, error_message, created_at, started_at, completed_at "
            "FROM draft_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def set_job_status(
        self,
        *,
        job_id: int,
        target: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        progress_percent: Optional[float] = None,
        allow_recovery: bool = False,
    ) -> None:
        """Move a job through its state machine and commit.

        Args:
            target: New status, validated against the transition table.
            error_code: Stable snake_case failure code (never raw exception text).
            error_message: Bounded, redacted diagnostic. Truncated on write.
            allow_recovery: Only startup recovery may pass True, permitting
                ``running -> pending``.
        """
        if target not in JOB_STATUSES:
            raise DraftValidationError(f"unknown job status: {target!r}")
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT status FROM draft_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("job not found")
            _check_transition(
                "job", row[0], target,
                _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS,
                allow_recovery=allow_recovery,
            )
            terminal = target in ("completed", "failed", "cancelled")
            self._db.execute(
                "UPDATE draft_jobs SET status = ?, error_code = ?, error_message = ?, "
                "progress_percent = COALESCE(?, progress_percent), "
                "heartbeat_at = CURRENT_TIMESTAMP, "
                "started_at = CASE WHEN ? = 'pending' THEN NULL ELSE started_at END, "
                "completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END "
                "WHERE id = ?",
                (
                    target,
                    error_code,
                    _truncate(error_message, _MAX_ERROR_MESSAGE_LEN),
                    progress_percent,
                    target,
                    1 if terminal else 0,
                    job_id,
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def request_job_cancel(
        self, *, draft_id: int, owner_id: int, job_id: int
    ) -> DraftJobRecord:
        """Cancel a job.

        A pending job goes straight to ``cancelled``. A running job gets a
        cancellation request that the worker observes cooperatively before it
        commits any output. Cancelling is allowed even after vault access is
        revoked, because it only reduces private processing.

        Raises:
            DraftConflictError: The job already reached a terminal state.
        """
        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            row = self._db.execute(
                "SELECT status FROM draft_jobs WHERE id = ? AND draft_id = ?",
                (job_id, draft_id),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("job not found")
            status = row[0]
            if status in ("completed", "failed", "cancelled"):
                raise DraftConflictError("job has already finished")
            if status == "pending":
                _check_transition(
                    "job", status, "cancelled",
                    _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS,
                )
                self._db.execute(
                    "UPDATE draft_jobs SET status = 'cancelled', "
                    "cancel_requested_at = CURRENT_TIMESTAMP, "
                    "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (job_id,),
                )
            else:
                self._db.execute(
                    "UPDATE draft_jobs SET cancel_requested_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND cancel_requested_at IS NULL",
                    (job_id,),
                )
            self._insert_event(
                draft_id=draft_id,
                event_type="job_cancelled",
                actor_user_id=owner_id,
                job_id=job_id,
                payload={"prior_status": status},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)

    def is_cancel_requested(self, job_id: int) -> bool:
        """True once a cancellation has been requested for this job."""
        row = self._db.execute(
            "SELECT cancel_requested_at FROM draft_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return bool(row is not None and row[0] is not None)

    def retry_parse_job(
        self, *, draft_id: int, owner_id: int, job_id: int, timeout_seconds: int
    ) -> DraftJobRecord:
        """Create a child retry job for a failed/cancelled parse job.

        The terminal job is preserved for audit and is never mutated back to
        pending; the retry is a new row carrying ``parent_job_id`` and
        ``attempt_no + 1``, and the input returns to ``pending``.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, None)
            row = self._db.execute(
                "SELECT status, job_type, input_id, attempt_no FROM draft_jobs "
                "WHERE id = ? AND draft_id = ?",
                (job_id, draft_id),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("job not found")
            status, job_type, input_id, attempt_no = row[0], row[1], row[2], int(row[3])
            if job_type != "parse_input":
                raise DraftValidationError("only parse jobs can be retried in this release")
            if status not in ("failed", "cancelled"):
                raise DraftConflictError("only a failed or cancelled job can be retried")
            if input_id is None:
                raise DraftValidationError("parse job has no input")

            input_row = self._db.execute(
                "SELECT parse_status FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            ).fetchone()
            if input_row is None:
                raise DraftNotFoundError("input not found")
            _check_transition(
                "input", input_row[0], "pending",
                _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE draft_inputs SET parse_status = 'pending', parse_error = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (input_id,),
            )
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                    "input_id, parent_job_id, attempt_no, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'parse_input', ?, ?, ?, 0, ?)",
                    (
                        draft_id,
                        draft.vault_id,
                        draft.created_by,
                        input_id,
                        job_id,
                        attempt_no + 1,
                        timeout_seconds,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    "a parse job is already active for this input"
                ) from exc
            new_job_id = int(cur.lastrowid)
            self._insert_event(
                draft_id=draft_id,
                event_type="job_retried",
                actor_user_id=owner_id,
                job_id=new_job_id,
                payload={"input_id": input_id, "parent_job_id": job_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_job(draft_id=draft_id, owner_id=owner_id, job_id=new_job_id)

    # ── startup recovery ─────────────────────────────────────────────────

    def recover_orphaned_parse_jobs(self) -> int:
        """Return crashed ``running`` parse jobs to ``pending`` at startup.

        A process that dies mid-parse leaves the job ``running`` and its input
        ``parsing`` forever. This resets both, marking the abandoned attempt with
        ``worker_restart`` so the audit trail shows why. Jobs whose cancellation
        was already requested go to ``cancelled`` instead of being resurrected.

        Returns:
            The number of jobs reset to pending.
        """
        self._begin_immediate()
        try:
            rows = self._db.execute(
                "SELECT id, input_id, cancel_requested_at FROM draft_jobs "
                "WHERE status = 'running' AND job_type = 'parse_input'"
            ).fetchall()
            reset = 0
            for row in rows:
                job_id, input_id, cancel_requested_at = row[0], row[1], row[2]
                if cancel_requested_at is not None:
                    self._db.execute(
                        "UPDATE draft_jobs SET status = 'cancelled', "
                        "error_code = 'worker_restart', "
                        "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (job_id,),
                    )
                    if input_id is not None:
                        self._db.execute(
                            "UPDATE draft_inputs SET parse_status = 'cancelled', "
                            "updated_at = CURRENT_TIMESTAMP "
                            "WHERE id = ? AND parse_status = 'parsing'",
                            (input_id,),
                        )
                    continue
                self._db.execute(
                    "UPDATE draft_jobs SET status = 'pending', started_at = NULL, "
                    "error_code = 'worker_restart', error_message = "
                    "'previous attempt abandoned by a worker restart' WHERE id = ?",
                    (job_id,),
                )
                if input_id is not None:
                    self._db.execute(
                        "UPDATE draft_inputs SET parse_status = 'pending', "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND parse_status = 'parsing'",
                        (input_id,),
                    )
                reset += 1
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        if reset:
            logger.warning(
                "draft room: reset %d orphaned parse job(s) to pending after restart",
                reset,
            )
        return reset

    def list_pending_inputs_without_active_job(self) -> list[tuple[int, int, int, str]]:
        """Inputs stuck ``pending`` with no active parse job.

        A crash between the reservation commit and the job enqueue leaves an input
        that would otherwise never be parsed. Startup reconciliation re-enqueues
        those whose bytes are present and fails those whose bytes are missing.

        Returns:
            Tuples of ``(input_id, draft_id, owner_id, storage_relpath)``.
        """
        rows = self._db.execute(
            "SELECT i.id, i.draft_id, d.created_by, i.storage_relpath "
            "FROM draft_inputs i JOIN drafts d ON d.id = i.draft_id "
            "WHERE i.parse_status = 'pending' AND NOT EXISTS ("
            "  SELECT 1 FROM draft_jobs j WHERE j.input_id = i.id "
            "  AND j.job_type = 'parse_input' AND j.status IN ('pending','running'))"
        ).fetchall()
        return [(int(r[0]), int(r[1]), int(r[2]), r[3]) for r in rows]

    def enqueue_parse_job_for_recovery(
        self, *, input_id: int, timeout_seconds: int
    ) -> Optional[int]:
        """Re-enqueue a parse job for an orphaned input during startup recovery.

        Returns the new job ID, or None when a job already exists (another
        process won the race) or the input has since disappeared.
        """
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT i.draft_id, d.vault_id, d.created_by FROM draft_inputs i "
                "JOIN drafts d ON d.id = i.draft_id WHERE i.id = ?",
                (input_id,),
            ).fetchone()
            if row is None:
                self._db.rollback()
                return None
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                    "input_id, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'parse_input', ?, 0, ?)",
                    (int(row[0]), int(row[1]), int(row[2]), input_id, timeout_seconds),
                )
            except sqlite3.IntegrityError:
                self._db.rollback()
                return None
            job_id = int(cur.lastrowid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return job_id

    # ── deletion support ─────────────────────────────────────────────────

    def list_input_relpaths(self, draft_id: int) -> list[str]:
        """Every stored relative path belonging to a draft (deletion planning)."""
        rows = self._db.execute(
            "SELECT storage_relpath FROM draft_inputs WHERE draft_id = ?",
            (draft_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def delete_draft_row(self, *, draft_id: int, owner_id: int) -> None:
        """Delete the project row; children cascade via foreign keys.

        Phase 2 of the whole-draft tombstone flow. Inputs, revisions, jobs, and
        project events all carry ``ON DELETE CASCADE`` to ``drafts``, so this one
        statement purges the derivative content. The content-free security-audit
        deletion event is recorded separately by the caller and survives.
        """
        self._begin_immediate()
        try:
            cur = self._db.execute(
                "DELETE FROM drafts WHERE id = ? AND created_by = ?",
                (draft_id, owner_id),
            )
            if cur.rowcount == 0:
                raise DraftNotFoundError("draft not found")
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def cancel_pending_jobs(self, *, draft_id: int, owner_id: int) -> int:
        """Cancel every pending job on a draft before deleting it.

        Running jobs are not force-terminated here: the delete route returns 409
        until the owner cancels them, so a worker can never write into rows that
        are being removed underneath it.

        Returns:
            The number of pending jobs cancelled.
        """
        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            cur = self._db.execute(
                "UPDATE draft_jobs SET status = 'cancelled', "
                "cancel_requested_at = CURRENT_TIMESTAMP, "
                "completed_at = CURRENT_TIMESTAMP "
                "WHERE draft_id = ? AND status = 'pending'",
                (draft_id,),
            )
            count = cur.rowcount
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return count

    def list_draft_ids_for_vault(self, vault_id: int) -> list[tuple[int, int]]:
        """``(draft_id, owner_id)`` for every draft in a vault (cascade cleanup)."""
        rows = self._db.execute(
            "SELECT id, created_by FROM drafts WHERE vault_id = ?", (vault_id,)
        ).fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

    def list_draft_ids_for_user(self, user_id: int) -> list[tuple[int, int]]:
        """``(draft_id, owner_id)`` for every draft owned by a user."""
        rows = self._db.execute(
            "SELECT id, created_by FROM drafts WHERE created_by = ?", (user_id,)
        ).fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

    def list_all_owner_draft_pairs(self) -> set[tuple[int, int]]:
        """Every ``(owner_id, draft_id)`` pair that currently exists.

        Startup filesystem reconciliation compares this against the directories
        under ``data/draft-room/`` so orphaned private bytes can be removed.
        """
        rows = self._db.execute("SELECT created_by, id FROM drafts").fetchall()
        return {(int(r[0]), int(r[1])) for r in rows}


def build_input_relpath(
    *, owner_id: int, draft_id: int, input_id: int, stored_name: str
) -> str:
    """Build the canonical relative storage path for an input.

    Layout (SPEC section 6.1), relative to ``data/draft-room``::

        <user_id>/<draft_id>/inputs/<input_id>/<uuid><extension>

    ``stored_name`` is a server-generated UUID filename — never the client's
    filename, which is retained only as display metadata.
    """
    return f"{owner_id}/{draft_id}/inputs/{input_id}/{stored_name}"


def canonical_json(payload: Any) -> str:
    """Serialize with sorted keys and fixed separators for stable hashing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def json_or_default(raw: Optional[str], default: Any) -> Any:
    """Parse stored JSON defensively, falling back to ``default`` when unusable."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def iter_chunks(values: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Yield ``values`` in lists of at most ``size`` items."""
    chunk: list[Any] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
