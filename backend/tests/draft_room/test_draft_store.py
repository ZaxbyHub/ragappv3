"""Store-level unit tests for DraftStore (issue #435, SPEC section 17.1).

Exercises app.services.draft_store.DraftStore directly against a real SQLite
database -- no HTTP, no FastAPI TestClient. Route-level behavior (auth,
serialization, request validation, HTTP status codes) is covered by
test_draft_routes.py; this file proves the store's own invariants: centralized status transitions,
manual-revision allocation/immutability, reserve_input's atomic limit and
duplicate checks, cross-draft/cross-owner child-ID rejection, owner/vault
immutability, and revision-number allocation under real concurrency.

Harness mirrors test_tags_routes.py / test_draft_job_processor.py: a temp
SQLite database via init_db + run_migrations, and _db_pool.SimpleConnectionPool
for connections (route-test convention; no HTTP is actually exercised here).
"""

import inspect
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from _db_pool import SimpleConnectionPool

from app.services.draft_store import (
    _DRAFT_RECOVERY_TRANSITIONS,
    _DRAFT_TRANSITIONS,
    _INPUT_RECOVERY_TRANSITIONS,
    _INPUT_TRANSITIONS,
    _JOB_RECOVERY_TRANSITIONS,
    _JOB_TRANSITIONS,
    DRAFT_STATUSES,
    INPUT_STATUSES,
    JOB_STATUSES,
    DraftConflictError,
    DraftLimitExceededError,
    DraftNotFoundError,
    DraftStore,
    InvalidTransitionError,
    _check_transition,
    build_input_relpath,
    sha256_text,
)

OWNER_ID = 90001
OTHER_ID = 90002
VAULT_ID = 90001
OTHER_VAULT_ID = 90002


class DraftStoreTestBase(unittest.TestCase):
    """Real temp SQLite DB + a live DraftStore bound to one pooled connection."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        self.pool = SimpleConnectionPool(self._db_path)
        self.conn = self.pool.get_connection()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, 'owner', 'hash', 'Owner', 'member', 1)",
            (OWNER_ID,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, 'other', 'hash', 'Other', 'member', 1)",
            (OTHER_ID,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, 'V1', '')",
            (VAULT_ID,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, 'V2', '')",
            (OTHER_VAULT_ID,),
        )
        self.conn.commit()

        self.store = DraftStore(self.conn)

    def tearDown(self):
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- helpers --

    def _make_draft(self, *, owner_id=OWNER_ID, vault_id=VAULT_ID, title="Draft"):
        return self.store.create_draft(
            vault_id=vault_id,
            created_by=owner_id,
            title=title,
            mode="compose",
            tier="standard",
            brief_json="{}",
        )

    def _reserve_input(
        self,
        draft_id,
        *,
        owner_id=OWNER_ID,
        content_sha256=None,
        size_bytes=100,
        max_inputs=100,
        max_total_input_bytes=10_000_000,
    ):
        return self.store.reserve_input(
            draft_id=draft_id,
            owner_id=owner_id,
            role="reference",
            authority="unknown",
            as_of_date=None,
            original_name="a.txt",
            stored_name=f"{uuid.uuid4()}.txt",
            extension=".txt",
            media_type="text/plain",
            size_bytes=size_bytes,
            content_sha256=content_sha256 or sha256_text(str(uuid.uuid4())),
            max_inputs=max_inputs,
            max_total_input_bytes=max_total_input_bytes,
        )


# ── Centralized transitions ────────────────────────────────────────────────


class TestDraftTransitionMatrix(DraftStoreTestBase):
    """Exercises the module-level _check_transition primitive that every
    status-mutating DraftStore method funnels through (draft_store.py's own
    docstring: "every status change goes through the transition tables below
    and is rejected otherwise"). Exhaustive over the small state spaces
    (8 draft / 5 job / 5 input statuses) rather than merely representative."""

    def test_all_legal_draft_transitions_succeed(self):
        for current, targets in _DRAFT_TRANSITIONS.items():
            for target in targets:
                _check_transition(
                    "draft", current, target, _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS
                )

    def test_all_illegal_draft_transitions_raise(self):
        for current in DRAFT_STATUSES:
            allowed = _DRAFT_TRANSITIONS.get(current, frozenset())
            for target in DRAFT_STATUSES:
                if target == current or target in allowed:
                    continue
                with self.assertRaises(InvalidTransitionError, msg=f"{current} -> {target}"):
                    _check_transition(
                        "draft", current, target, _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS
                    )

    def test_all_legal_job_transitions_succeed(self):
        for current, targets in _JOB_TRANSITIONS.items():
            for target in targets:
                _check_transition(
                    "job", current, target, _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS
                )

    def test_all_illegal_job_transitions_raise(self):
        for current in JOB_STATUSES:
            allowed = _JOB_TRANSITIONS.get(current, frozenset())
            for target in JOB_STATUSES:
                if target == current or target in allowed:
                    continue
                with self.assertRaises(InvalidTransitionError, msg=f"{current} -> {target}"):
                    _check_transition(
                        "job", current, target, _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS
                    )

    def test_all_legal_input_transitions_succeed(self):
        for current, targets in _INPUT_TRANSITIONS.items():
            for target in targets:
                _check_transition(
                    "input", current, target, _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS
                )

    def test_all_illegal_input_transitions_raise(self):
        for current in INPUT_STATUSES:
            allowed = _INPUT_TRANSITIONS.get(current, frozenset())
            for target in INPUT_STATUSES:
                if target == current or target in allowed:
                    continue
                with self.assertRaises(InvalidTransitionError, msg=f"{current} -> {target}"):
                    _check_transition(
                        "input", current, target, _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS
                    )

    def test_recovery_only_edges_rejected_without_allow_recovery(self):
        with self.assertRaises(InvalidTransitionError):
            _check_transition(
                "draft", "running", "queued", _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS
            )
        with self.assertRaises(InvalidTransitionError):
            _check_transition(
                "job", "running", "pending", _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS
            )
        with self.assertRaises(InvalidTransitionError):
            _check_transition(
                "input", "parsing", "pending", _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS
            )

    def test_recovery_only_edges_accepted_with_allow_recovery(self):
        _check_transition(
            "draft", "running", "queued", _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
            allow_recovery=True,
        )
        _check_transition(
            "job", "running", "pending", _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS,
            allow_recovery=True,
        )
        _check_transition(
            "input", "parsing", "pending", _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS,
            allow_recovery=True,
        )

    def test_allow_recovery_does_not_open_unrelated_illegal_targets(self):
        # allow_recovery only widens the allowed set by the recovery table's
        # own entries -- it must not make every other transition legal too.
        with self.assertRaises(InvalidTransitionError):
            _check_transition(
                "draft", "running", "archived", _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
                allow_recovery=True,
            )
        with self.assertRaises(InvalidTransitionError):
            _check_transition(
                "job", "completed", "pending", _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS,
                allow_recovery=True,
            )


class TestJobStatusTransitionsAtStoreLevel(DraftStoreTestBase):
    """Proves the real public wiring (enqueue_parse_job / set_job_status), not
    just the transition table in isolation."""

    def _job(self):
        draft = self._make_draft()
        inp = self._reserve_input(draft.id)
        return self.store.enqueue_parse_job(
            draft_id=draft.id, owner_id=OWNER_ID, input_id=inp.id, timeout_seconds=60
        )

    def test_legal_chain_pending_running_completed(self):
        job = self._job()
        self.store.set_job_status(job_id=job.id, target="running")
        reloaded = self.store.get_job(draft_id=job.draft_id, owner_id=OWNER_ID, job_id=job.id)
        self.assertEqual(reloaded.status, "running")

        self.store.set_job_status(job_id=job.id, target="completed")
        reloaded = self.store.get_job(draft_id=job.draft_id, owner_id=OWNER_ID, job_id=job.id)
        self.assertEqual(reloaded.status, "completed")
        self.assertIsNotNone(reloaded.completed_at)

    def test_illegal_pending_to_completed_raises_and_db_unchanged(self):
        job = self._job()
        with self.assertRaises(InvalidTransitionError):
            self.store.set_job_status(job_id=job.id, target="completed")
        reloaded = self.store.get_job(draft_id=job.draft_id, owner_id=OWNER_ID, job_id=job.id)
        self.assertEqual(reloaded.status, "pending")

    def test_terminal_status_has_no_further_transitions(self):
        job = self._job()
        self.store.set_job_status(job_id=job.id, target="running")
        self.store.set_job_status(job_id=job.id, target="failed")
        with self.assertRaises(InvalidTransitionError):
            self.store.set_job_status(job_id=job.id, target="pending", allow_recovery=True)

    def test_running_to_pending_recovery_edge(self):
        job = self._job()
        self.store.set_job_status(job_id=job.id, target="running")
        with self.assertRaises(InvalidTransitionError):
            self.store.set_job_status(job_id=job.id, target="pending")
        self.store.set_job_status(job_id=job.id, target="pending", allow_recovery=True)
        reloaded = self.store.get_job(draft_id=job.draft_id, owner_id=OWNER_ID, job_id=job.id)
        self.assertEqual(reloaded.status, "pending")


class TestInputParseTransitionsAtStoreLevel(DraftStoreTestBase):
    def test_legal_chain_pending_parsing_ready(self):
        draft = self._make_draft()
        inp = self._reserve_input(draft.id)
        self.store.set_input_parse_status(input_id=inp.id, target="parsing")
        self.store.set_input_parse_status(
            input_id=inp.id, target="ready", parsed_text="hi", parsed_text_sha256=sha256_text("hi"),
            parsed_char_count=2,
        )
        reloaded = self.store.get_input(draft_id=draft.id, owner_id=OWNER_ID, input_id=inp.id)
        self.assertEqual(reloaded.parse_status, "ready")

    def test_illegal_pending_to_ready_raises(self):
        draft = self._make_draft()
        inp = self._reserve_input(draft.id)
        with self.assertRaises(InvalidTransitionError):
            self.store.set_input_parse_status(input_id=inp.id, target="ready")

    def test_parsing_to_pending_recovery_edge(self):
        draft = self._make_draft()
        inp = self._reserve_input(draft.id)
        self.store.set_input_parse_status(input_id=inp.id, target="parsing")
        with self.assertRaises(InvalidTransitionError):
            self.store.set_input_parse_status(input_id=inp.id, target="pending")
        self.store.set_input_parse_status(input_id=inp.id, target="pending", allow_recovery=True)
        reloaded = self.store.get_input(draft_id=draft.id, owner_id=OWNER_ID, input_id=inp.id)
        self.assertEqual(reloaded.parse_status, "pending")

    def test_failed_and_cancelled_may_return_to_pending_without_recovery(self):
        draft = self._make_draft()
        inp = self._reserve_input(draft.id)
        self.store.set_input_parse_status(input_id=inp.id, target="parsing")
        self.store.set_input_parse_status(input_id=inp.id, target="failed", parse_error="boom")
        # failed -> pending is an ordinary transition, not recovery-only.
        self.store.set_input_parse_status(input_id=inp.id, target="pending")
        reloaded = self.store.get_input(draft_id=draft.id, owner_id=OWNER_ID, input_id=inp.id)
        self.assertEqual(reloaded.parse_status, "pending")


class TestDraftStatusTransitionsAtStoreLevel(DraftStoreTestBase):
    """archive_draft/restore_draft/create_manual_revision are the only public
    methods that mutate drafts.status; the compile lifecycle (queued/running)
    has no store-level setter in this release, so those edges are covered
    exhaustively above via the transition table directly."""

    def test_restore_draft_requires_archived_status(self):
        draft = self._make_draft()
        with self.assertRaises(InvalidTransitionError):
            self.store.restore_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version)

    def test_archive_draft_illegal_from_unreachable_running_status(self):
        draft = self._make_draft()
        # 'running' has no public setter in this release (compile lifecycle is
        # not implemented yet); set it directly to exercise the guard.
        self.conn.execute("UPDATE drafts SET status = 'running' WHERE id = ?", (draft.id,))
        self.conn.commit()
        with self.assertRaises(InvalidTransitionError):
            self.store.archive_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version)


# ── create_manual_revision ─────────────────────────────────────────────────


class TestCreateManualRevision(DraftStoreTestBase):
    def test_revision_no_allocation_and_parent_linkage(self):
        draft = self._make_draft()
        rev1 = self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version,
            base_revision_id=None, content_md="v1",
        )
        self.assertEqual(rev1.revision_no, 1)
        self.assertIsNone(rev1.parent_revision_id)

        draft2 = self.store.get_draft(draft.id, OWNER_ID)
        rev2 = self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft2.lock_version,
            base_revision_id=rev1.id, content_md="v2",
        )
        self.assertEqual(rev2.revision_no, 2)
        self.assertEqual(rev2.parent_revision_id, rev1.id)

    def test_exactly_one_is_current_and_prior_immutable(self):
        draft = self._make_draft()
        rev1 = self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version,
            base_revision_id=None, content_md="v1",
        )
        draft2 = self.store.get_draft(draft.id, OWNER_ID)
        self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft2.lock_version,
            base_revision_id=rev1.id, content_md="v2",
        )

        count = self.conn.execute(
            "SELECT COUNT(*) FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
            (draft.id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)

        rev1_reloaded = self.store.get_revision(draft_id=draft.id, owner_id=OWNER_ID, revision_id=rev1.id)
        self.assertEqual(rev1_reloaded.content_md, "v1")
        self.assertFalse(rev1_reloaded.is_current)

    def test_draft_moves_to_needs_review_and_lock_bumped(self):
        draft = self._make_draft()
        self.assertEqual(draft.status, "draft")
        rev1 = self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version,
            base_revision_id=None, content_md="v1",
        )
        draft_after = self.store.get_draft(draft.id, OWNER_ID)
        self.assertEqual(draft_after.status, "needs_review")
        self.assertEqual(draft_after.lock_version, draft.lock_version + 1)
        self.assertIsNotNone(rev1.id)

    def test_ready_fields_cleared_on_new_revision(self):
        draft = self._make_draft()
        rev1 = self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version,
            base_revision_id=None, content_md="v1",
        )
        # Simulate a prior Ready approval: no store method sets 'ready' in this
        # release (promote_available is False), so set it directly for the test.
        self.conn.execute(
            "UPDATE drafts SET status = 'ready', ready_revision_id = ?, ready_by = ?, "
            "ready_at = CURRENT_TIMESTAMP WHERE id = ?",
            (rev1.id, OWNER_ID, draft.id),
        )
        self.conn.commit()
        draft_ready = self.store.get_draft(draft.id, OWNER_ID)
        self.assertIsNotNone(draft_ready.ready_revision_id)

        self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft_ready.lock_version,
            base_revision_id=rev1.id, content_md="v2",
        )
        draft_after = self.store.get_draft(draft.id, OWNER_ID)
        self.assertIsNone(draft_after.ready_revision_id)
        self.assertIsNone(draft_after.ready_by)
        self.assertIsNone(draft_after.ready_at)
        self.assertEqual(draft_after.status, "needs_review")

    def test_stale_lock_version_raises_conflict(self):
        draft = self._make_draft()
        with self.assertRaises(DraftConflictError):
            self.store.create_manual_revision(
                draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version + 99,
                base_revision_id=None, content_md="v1",
            )

    def test_wrong_base_revision_id_raises_conflict(self):
        draft = self._make_draft()
        with self.assertRaises(DraftConflictError):
            self.store.create_manual_revision(
                draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version,
                base_revision_id=999999, content_md="v1",
            )


# ── reserve_input invariants ───────────────────────────────────────────────


class TestReserveInputInvariants(DraftStoreTestBase):
    def test_duplicate_content_sha256_in_same_draft_raises_with_existing_id(self):
        draft = self._make_draft()
        content_hash = sha256_text("same bytes")
        first = self._reserve_input(draft.id, content_sha256=content_hash)

        with self.assertRaises(DraftConflictError) as ctx:
            self._reserve_input(draft.id, content_sha256=content_hash)
        err = ctx.exception
        self.assertEqual(err.code, "duplicate_input")
        self.assertEqual(err.existing_input_id, first.id)

    def test_same_hash_in_different_draft_is_allowed(self):
        draft_a = self._make_draft(title="A")
        draft_b = self._make_draft(title="B")
        content_hash = sha256_text("shared bytes")
        first = self._reserve_input(draft_a.id, content_sha256=content_hash)
        second = self._reserve_input(draft_b.id, content_sha256=content_hash)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_max_inputs_limit_raises(self):
        draft = self._make_draft()
        self._reserve_input(draft.id, max_inputs=1)
        with self.assertRaises(DraftLimitExceededError):
            self._reserve_input(draft.id, max_inputs=1)

    def test_max_total_bytes_limit_raises(self):
        draft = self._make_draft()
        self._reserve_input(draft.id, size_bytes=600, max_total_input_bytes=1000)
        with self.assertRaises(DraftLimitExceededError):
            self._reserve_input(draft.id, size_bytes=500, max_total_input_bytes=1000)

    def test_storage_relpath_derived_from_lastrowid(self):
        draft = self._make_draft()
        stored_name = f"{uuid.uuid4()}.txt"
        record = self.store.reserve_input(
            draft_id=draft.id,
            owner_id=OWNER_ID,
            role="reference",
            authority="unknown",
            as_of_date=None,
            original_name="a.txt",
            stored_name=stored_name,
            extension=".txt",
            media_type="text/plain",
            size_bytes=10,
            content_sha256=sha256_text("relpath check"),
            max_inputs=100,
            max_total_input_bytes=10_000_000,
        )
        expected = build_input_relpath(
            owner_id=OWNER_ID, draft_id=draft.id, input_id=record.id, stored_name=stored_name
        )
        self.assertEqual(record.storage_relpath, expected)


# ── cross-draft / cross-owner child-ID rejection ───────────────────────────


class TestCrossDraftCrossOwnerRejection(DraftStoreTestBase):
    def setUp(self):
        super().setUp()
        self.draft_a = self._make_draft(title="A")
        self.draft_b = self._make_draft(title="B")
        self.input_b = self._reserve_input(self.draft_b.id)
        self.job_b = self.store.enqueue_parse_job(
            draft_id=self.draft_b.id, owner_id=OWNER_ID, input_id=self.input_b.id, timeout_seconds=60
        )
        self.revision_b = self.store.create_manual_revision(
            draft_id=self.draft_b.id, owner_id=OWNER_ID, lock_version=self.draft_b.lock_version,
            base_revision_id=None, content_md="body",
        )

    def test_get_input_rejects_child_id_from_different_draft(self):
        with self.assertRaises(DraftNotFoundError):
            self.store.get_input(draft_id=self.draft_a.id, owner_id=OWNER_ID, input_id=self.input_b.id)

    def test_get_input_rejects_correct_id_wrong_owner(self):
        with self.assertRaises(DraftNotFoundError):
            self.store.get_input(draft_id=self.draft_b.id, owner_id=OTHER_ID, input_id=self.input_b.id)

    def test_get_job_rejects_child_id_from_different_draft(self):
        with self.assertRaises(DraftNotFoundError):
            self.store.get_job(draft_id=self.draft_a.id, owner_id=OWNER_ID, job_id=self.job_b.id)

    def test_get_job_rejects_correct_id_wrong_owner(self):
        with self.assertRaises(DraftNotFoundError):
            self.store.get_job(draft_id=self.draft_b.id, owner_id=OTHER_ID, job_id=self.job_b.id)

    def test_get_revision_rejects_child_id_from_different_draft(self):
        with self.assertRaises(DraftNotFoundError):
            self.store.get_revision(draft_id=self.draft_a.id, owner_id=OWNER_ID, revision_id=self.revision_b.id)

    def test_get_revision_rejects_correct_id_wrong_owner(self):
        with self.assertRaises(DraftNotFoundError):
            self.store.get_revision(draft_id=self.draft_b.id, owner_id=OTHER_ID, revision_id=self.revision_b.id)


class TestGetDraftNonOwner(DraftStoreTestBase):
    def test_get_draft_for_non_owner_raises_not_found(self):
        draft = self._make_draft()
        with self.assertRaises(DraftNotFoundError):
            self.store.get_draft(draft.id, OTHER_ID)


# ── owner/vault immutability ────────────────────────────────────────────────


class TestOwnerVaultImmutability(DraftStoreTestBase):
    """Verified two ways: (1) the public method surface has no name suggesting
    an owner/vault mutator, and (2) no UPDATE statement anywhere in the store's
    source ever assigns `created_by` or `vault_id` in its SET clause (only in
    WHERE clauses, which scope reads/writes to the existing owner)."""

    def test_no_public_method_name_suggests_owner_or_vault_mutation(self):
        method_names = [
            name
            for name, _ in inspect.getmembers(DraftStore, predicate=inspect.isfunction)
            if not name.startswith("_")
        ]
        suspicious_terms = ("owner", "vault", "transfer", "move")
        # create_draft legitimately sets vault_id/created_by at INSERT time.
        # list_all_owner_draft_pairs / list_draft_ids_for_vault / list_draft_ids_for_user
        # are read-only lookups (startup reconciliation, cascade cleanup) that
        # reference owner/vault in their name but never write those columns --
        # confirmed by reading their bodies (draft_store.py: pure SELECTs).
        # No other public method name should reference either concept.
        allowed = {
            "create_draft",
            "list_all_owner_draft_pairs",
            "list_draft_ids_for_vault",
            "list_draft_ids_for_user",
        }
        flagged = [
            name
            for name in method_names
            if name not in allowed
            and any(term in name.lower() for term in suspicious_terms)
        ]
        self.assertEqual(flagged, [], f"unexpected owner/vault-mutating method names: {flagged}")

    def test_no_update_statement_assigns_created_by_or_vault_id(self):
        source = inspect.getsource(DraftStore)
        for match in re.finditer(r"UPDATE\s+\w+\s+SET(.*?)WHERE", source, re.DOTALL):
            set_clause = match.group(1)
            self.assertNotIn("created_by =", set_clause)
            self.assertNotIn("created_by=", set_clause)
            self.assertNotIn("vault_id =", set_clause)
            self.assertNotIn("vault_id=", set_clause)

    def test_created_by_and_vault_id_survive_every_public_mutation(self):
        draft = self._make_draft()
        rev1 = self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version,
            base_revision_id=None, content_md="v1",
        )
        self.store.update_draft(
            draft_id=draft.id, owner_id=OWNER_ID,
            lock_version=self.store.get_draft(draft.id, OWNER_ID).lock_version,
            title="Renamed",
        )
        current = self.store.get_draft(draft.id, OWNER_ID)
        self.store.archive_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=current.lock_version)
        archived = self.store.get_draft(draft.id, OWNER_ID)
        self.store.restore_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=archived.lock_version)

        final = self.store.get_draft(draft.id, OWNER_ID)
        self.assertEqual(final.created_by, OWNER_ID)
        self.assertEqual(final.vault_id, VAULT_ID)
        self.assertIsNotNone(rev1.id)


# ── Finding 2: archive retains input bytes ─────────────────────────────────


class TestArchiveRetainsInputs(DraftStoreTestBase):
    def test_archive_retains_input_file_and_row(self):
        draft = self._make_draft()
        stored_name = f"{uuid.uuid4()}.txt"
        record = self.store.reserve_input(
            draft_id=draft.id,
            owner_id=OWNER_ID,
            role="reference",
            authority="unknown",
            as_of_date=None,
            original_name="a.txt",
            stored_name=stored_name,
            extension=".txt",
            media_type="text/plain",
            size_bytes=11,
            content_sha256=sha256_text("hello world"),
            max_inputs=100,
            max_total_input_bytes=10_000_000,
        )
        storage_root = Path(self._temp_dir) / "draft-room"
        file_path = storage_root / record.storage_relpath
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"hello world")
        self.assertTrue(file_path.exists())

        self.store.archive_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version)

        # File bytes on disk untouched.
        self.assertTrue(file_path.exists())
        self.assertEqual(file_path.read_bytes(), b"hello world")

        # draft_inputs row untouched.
        row = self.conn.execute(
            "SELECT id FROM draft_inputs WHERE id = ? AND draft_id = ?",
            (record.id, draft.id),
        ).fetchone()
        self.assertIsNotNone(row)

        archived = self.store.get_draft(draft.id, OWNER_ID)
        self.assertEqual(archived.status, "archived")


# ── Finding 3: revision-number allocation under concurrency ────────────────


class TestConcurrentManualRevisions(DraftStoreTestBase):
    """N real threads, each with its own SQLite connection, race to save a
    manual revision against the same draft with the same base_revision_id and
    lock_version. Because create_manual_revision requires base_revision_id to
    match the current revision AND lock_version to match exactly, at most one
    caller can win: everyone else must observe a stale lock_version (or a
    stale base pointer) the instant the winner commits. This mirrors
    test_draft_job_processor.py's real-threads claim-atomicity test."""

    N_THREADS = 8

    def test_concurrent_saves_leave_a_consistent_single_winner(self):
        draft = self._make_draft()
        rev1 = self.store.create_manual_revision(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version,
            base_revision_id=None, content_md="base",
        )
        draft_after_rev1 = self.store.get_draft(draft.id, OWNER_ID)
        shared_lock_version = draft_after_rev1.lock_version

        results: list[object] = [None] * self.N_THREADS
        barrier = threading.Barrier(self.N_THREADS)

        def attempt(slot: int) -> None:
            conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            store = DraftStore(conn)
            try:
                # Generous: this only bounds how long threads wait to line up,
                # not the contention window under test. A loaded CI runner can
                # take seconds to schedule 8 threads, and a BrokenBarrierError
                # here would fail the test for reasons unrelated to the race.
                barrier.wait(timeout=60)
                try:
                    rev = store.create_manual_revision(
                        draft_id=draft.id,
                        owner_id=OWNER_ID,
                        lock_version=shared_lock_version,
                        base_revision_id=rev1.id,
                        content_md=f"concurrent-{slot}",
                    )
                    results[slot] = ("ok", rev)
                except (DraftConflictError, InvalidTransitionError) as exc:
                    results[slot] = ("conflict", exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(self.N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for slot, result in enumerate(results):
            self.assertIsNotNone(result, f"thread {slot} never completed")

        successes = [r for kind, r in results if kind == "ok"]
        conflicts = [r for kind, r in results if kind == "conflict"]
        self.assertEqual(len(successes) + len(conflicts), self.N_THREADS)

        # With one fixed lock_version shared by every racer, exactly one
        # caller can win -- everyone else loses to the winner's lock bump.
        self.assertEqual(len(successes), 1, f"expected exactly one winner, got {len(successes)}")

        # Invariant check against real DB state, independent of the in-memory
        # thread results.
        rows = self.conn.execute(
            "SELECT revision_no, is_current FROM draft_revisions WHERE draft_id = ? ORDER BY revision_no",
            (draft.id,),
        ).fetchall()
        revision_nos = [r[0] for r in rows]
        self.assertEqual(len(revision_nos), len(set(revision_nos)), "duplicate revision_no")
        self.assertEqual(revision_nos, list(range(1, len(revision_nos) + 1)), "gap in revision_no sequence")
        self.assertEqual(len(revision_nos), 2)  # rev1 (base) + exactly one winner

        current_rows = [r for r in rows if r[1] == 1]
        self.assertEqual(len(current_rows), 1, "expected exactly one is_current row")


if __name__ == "__main__":
    unittest.main()
