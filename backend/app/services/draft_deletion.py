"""Two-phase tombstone deletion for Draft Room (issue #435, SPEC section 6.1/6.3).

Bytes and database rows must never disagree. Every deletion path here follows
the same order: tombstone the file(s) first, delete the owning row(s) in a
separate transaction, and — only if that transaction fails — restore every
tombstone and re-raise. The tombstone is permanently discarded only after the
transaction commits.

For vault/user deletion (SPEC section 6.1), the "owning row(s)" are not a
draft row at all but the parent vault/user row, deleted by the caller's own
handler transaction — the drafts cascade via ``ON DELETE CASCADE`` when that
row goes. Since that transaction is out of this module's hands, the two-phase
flow is split into three explicit steps the caller drives itself:
``prepare_purge_for_vault``/``prepare_purge_for_user`` (tombstone, before the
caller's transaction), ``commit_purge`` (discard the tombstones, after the
caller's transaction COMMITS), and ``restore_purge`` (undo the tombstones, if
the caller's transaction rolls back instead).

This module never reads manuscript text and never logs a relative/absolute
path, only IDs and counts.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field

from app.services.draft_input_storage import DraftInputPathError, DraftInputStorage
from app.services.draft_store import DraftConflictError, DraftStore

logger = logging.getLogger(__name__)


@dataclass
class PurgePlan:
    """Tombstones gathered before a parent (vault/user) row delete.

    Carries exactly what ``commit_purge``/``restore_purge`` need and nothing
    else — no manuscript text, no absolute paths.
    """

    tokens: list[tuple[str, str]] = field(default_factory=list)
    """``(token, relpath)`` pairs for every tombstoned input file."""

    draft_owner_pairs: list[tuple[int, int]] = field(default_factory=list)
    """``(draft_id, owner_id)`` for every draft the plan covers, so
    ``commit_purge`` can drop each draft's now-empty project directory."""


class DraftDeletionService:
    """Orchestrates tombstone-then-delete-row(s) across storage and the store."""

    def __init__(self, storage: DraftInputStorage) -> None:
        self._storage = storage

    # ── single input ─────────────────────────────────────────────────────

    def delete_input(
        self, store: DraftStore, *, draft_id: int, owner_id: int, input_id: int
    ) -> None:
        """Delete one input's bytes and row, refusing while it is in use.

        Raises:
            DraftNotFoundError: The input does not exist for this owner.
            DraftConflictError: ``code='input_in_use'`` — a completed compile
                used this input, or a parse job is currently active for it.
        """
        if store.input_is_in_use(draft_id=draft_id, input_id=input_id):
            err = DraftConflictError("input has been used by a completed artifact")
            err.code = "input_in_use"
            raise err
        if store.get_active_parse_job_id(input_id) is not None:
            err = DraftConflictError("input has an active parse job")
            err.code = "input_in_use"
            raise err

        record = store.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)
        relpath = record.storage_relpath

        token = self._storage.tombstone(relpath)
        try:
            store.delete_input_row(draft_id=draft_id, owner_id=owner_id, input_id=input_id)
        except Exception:
            self._storage.restore_tombstone(token, relpath)
            raise
        self._storage.commit_tombstone(token)
        logger.info("draft_input_deleted draft_id=%s input_id=%s", draft_id, input_id)

    # ── whole draft ──────────────────────────────────────────────────────

    def delete_draft(self, store: DraftStore, *, draft_id: int, owner_id: int) -> None:
        """Purge every input file and cascade-delete the draft's DB rows.

        Cancels pending jobs first. Raises ``DraftConflictError`` if a running
        job remains — the caller returns 409 until the owner cancels it.
        """
        store.cancel_pending_jobs(draft_id=draft_id, owner_id=owner_id)
        if store.count_active_jobs(draft_id) > 0:
            err = DraftConflictError("a running job must be cancelled before deletion")
            err.code = "active_job"
            raise err

        relpaths = store.list_input_relpaths(draft_id)
        tokens: list[tuple[str, str]] = []
        try:
            for relpath in relpaths:
                if not self._storage.exists(relpath):
                    continue
                token = self._storage.tombstone(relpath)
                tokens.append((token, relpath))
        except Exception:
            self._restore_all(tokens)
            raise

        try:
            store.delete_draft_row(draft_id=draft_id, owner_id=owner_id)
        except Exception:
            self._restore_all(tokens)
            raise

        for token, _relpath in tokens:
            self._storage.commit_tombstone(token)

        # The project's input files are gone; drop the now-empty
        # "<owner_id>/<draft_id>" tree so no orphan directory is left behind
        # (startup reconciliation would otherwise remove it later, but a
        # whole-draft delete is a comprehensive purge in its own right).
        try:
            project_dir = self._storage.resolve(f"{owner_id}/{draft_id}")
            if project_dir.is_dir():
                shutil.rmtree(project_dir, ignore_errors=True)
        except DraftInputPathError:
            logger.warning("draft_project_dir_cleanup_skipped draft_id=%s", draft_id)

        logger.info("draft_deleted draft_id=%s input_count=%s", draft_id, len(tokens))

    # ── vault/user cascades (prepare / commit / restore) ───────────────────

    def prepare_purge_for_vault(self, store: DraftStore, vault_id: int) -> PurgePlan:
        """Tombstone every input file for every draft in a vault.

        Call this *before* the caller's own transaction that deletes the
        vault row. Touches no draft rows and opens no database transaction —
        it is pure filesystem staging, so it cannot nest inside (or conflict
        with) the caller's later ``BEGIN IMMEDIATE``.
        """
        pairs = store.list_draft_ids_for_vault(vault_id)
        return self._prepare_purge(store, pairs)

    def prepare_purge_for_user(self, store: DraftStore, user_id: int) -> PurgePlan:
        """Tombstone every input file for every draft owned by a user.

        Same contract as :meth:`prepare_purge_for_vault`, scoped to one owner.
        """
        pairs = store.list_draft_ids_for_user(user_id)
        return self._prepare_purge(store, pairs)

    def _prepare_purge(
        self, store: DraftStore, pairs: list[tuple[int, int]]
    ) -> PurgePlan:
        tokens: list[tuple[str, str]] = []
        try:
            for draft_id, _owner_id in pairs:
                for relpath in store.list_input_relpaths(draft_id):
                    if not self._storage.exists(relpath):
                        continue
                    token = self._storage.tombstone(relpath)
                    tokens.append((token, relpath))
        except Exception:
            self._restore_all(tokens)
            raise
        return PurgePlan(tokens=tokens, draft_owner_pairs=list(pairs))

    def commit_purge(self, plan: PurgePlan) -> None:
        """Permanently discard every tombstone in ``plan`` and drop empty project dirs.

        Call only after the caller's parent-row-delete transaction COMMITS —
        the drafts (and their rows) are already gone at that point via
        ``ON DELETE CASCADE``, so this step is pure filesystem cleanup.
        """
        for token, _relpath in plan.tokens:
            self._storage.commit_tombstone(token)
        for draft_id, owner_id in plan.draft_owner_pairs:
            try:
                project_dir = self._storage.resolve(f"{owner_id}/{draft_id}")
                if project_dir.is_dir():
                    shutil.rmtree(project_dir, ignore_errors=True)
            except DraftInputPathError:
                logger.warning(
                    "draft_project_dir_cleanup_skipped draft_id=%s", draft_id
                )
        logger.info(
            "draft_purge_committed draft_count=%s input_count=%s",
            len(plan.draft_owner_pairs),
            len(plan.tokens),
        )

    def restore_purge(self, plan: PurgePlan) -> None:
        """Undo every tombstone in ``plan``.

        Call when the caller's parent-row-delete transaction rolls back (or
        anything raises before it commits), so surviving drafts keep their
        bytes.
        """
        self._restore_all(plan.tokens)

    # ── helpers ──────────────────────────────────────────────────────────

    def _restore_all(self, tokens: list[tuple[str, str]]) -> None:
        for token, relpath in tokens:
            try:
                self._storage.restore_tombstone(token, relpath)
            except Exception:
                logger.error("draft_tombstone_restore_failed")
