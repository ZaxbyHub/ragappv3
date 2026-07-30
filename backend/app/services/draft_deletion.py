"""Two-phase tombstone deletion for Draft Room (issue #435, SPEC section 6.1/6.3).

Bytes and database rows must never disagree. Every deletion path here follows
the same order: tombstone the file(s) first, delete the owning row(s) in a
separate transaction, and — only if that transaction fails — restore every
tombstone and re-raise. The tombstone is permanently discarded only after the
transaction commits.

This module never reads manuscript text and never logs a relative/absolute
path, only IDs and counts.
"""

from __future__ import annotations

import logging

from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_store import DraftConflictError, DraftStore

logger = logging.getLogger(__name__)


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
        logger.info("draft_deleted draft_id=%s input_count=%s", draft_id, len(tokens))

    # ── cascades ─────────────────────────────────────────────────────────

    def delete_drafts_for_vault(self, store: DraftStore, vault_id: int) -> int:
        """Purge every draft in a vault. Resilient: one failure does not abort the rest."""
        pairs = store.list_draft_ids_for_vault(vault_id)
        return self._delete_pairs(store, pairs)

    def delete_drafts_for_user(self, store: DraftStore, user_id: int) -> int:
        """Purge every draft owned by a user. Resilient: one failure does not abort the rest."""
        pairs = store.list_draft_ids_for_user(user_id)
        return self._delete_pairs(store, pairs)

    def _delete_pairs(
        self, store: DraftStore, pairs: list[tuple[int, int]]
    ) -> int:
        purged = 0
        failed = 0
        for draft_id, owner_id in pairs:
            try:
                self.delete_draft(store, draft_id=draft_id, owner_id=owner_id)
                purged += 1
            except Exception:
                failed += 1
                logger.warning(
                    "draft_cascade_delete_failed draft_id=%s", draft_id
                )
        if failed:
            logger.warning(
                "draft_cascade_delete_partial purged=%s failed=%s", purged, failed
            )
        return purged

    # ── helpers ──────────────────────────────────────────────────────────

    def _restore_all(self, tokens: list[tuple[str, str]]) -> None:
        for token, relpath in tokens:
            try:
                self._storage.restore_tombstone(token, relpath)
            except Exception:
                logger.error("draft_tombstone_restore_failed")
