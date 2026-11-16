"""Arm's-length acceptance checks for issue #916 (trace
1135-926-916-draft-room-residuals): the typed promotion cleanup-incomplete
branches and their operator log line.

The three typed promotion branches (Duplicate 409 / TooLarge 413 /
PathConflict 409) call the audit recorder when ``cleanup_incomplete`` is
set — an orphaned ``files``/``draft_promotions`` row or on-disk file may
genuinely exist — and that compensation failure must also be visible in
the process log operators tail. Checks are frozen BEFORE any fix exists
and replayed against both ends:

* AC10 (positive arm) — each typed branch with ``cleanup_incomplete=True``
  emits exactly ONE ERROR-level record on the route logger carrying
  ``draft_id`` and the error class name only, never the exception message
  text (the #789 leak class). RED at base (no such record exists on any
  typed branch; PathConflict only warns, in a different shape), GREEN
  after the fix.
* AC11 (negative arm, same module) — with ``cleanup_incomplete=False`` the
  typed branches stay out of the error channel entirely. GREEN at both
  ends; the module's RED at base comes from the positive arm.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

import app.api.routes.draft_room as draft_room_module  # noqa: E402
from app.services.draft_promotion import (  # noqa: E402
    DraftPromotionDuplicateError,
    DraftPromotionPathConflictError,
    DraftPromotionTooLargeError,
)
from tests.draft_room.test_draft_promote import DraftPromoteTestBase  # noqa: E402

#: The route module's own logger (``logging.getLogger(__name__)``).
ROUTE_LOGGER = "app.api.routes.draft_room"

#: Text embedded ONLY in the synthetic exception's message. The ERROR
#: record must carry the class name, never this text (#789 leak class).
_POISON_MESSAGE = "pool-exhausted-while-rolling-back-file-9127"

#: (label, exception class, expected status, expected response code).
_TYPED_BRANCHES = (
    ("duplicate", DraftPromotionDuplicateError, 409, "duplicate_document"),
    ("too_large", DraftPromotionTooLargeError, 413, "promotion_too_large"),
    ("path_conflict", DraftPromotionPathConflictError, 409, "promotion_path_conflict"),
)


class TestTypedCleanupBranchesLogError(DraftPromoteTestBase):
    """AC10: cleanup-incomplete typed branches land in the error channel.

    RED at base, GREEN after the fix."""

    longMessage = False

    def _ready_draft_with_input(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return draft_id, input_id

    def _promote_with_typed_error(self, exc):
        """Drive the promote route with the promotion service raising ``exc``
        (the typed error is injected at the service boundary; every route
        gate before it — ownership, vault write, worker liveness, input
        readiness — is exercised for real)."""
        draft_id, input_id = self._ready_draft_with_input()
        exc.cleanup_incomplete = True
        with patch.object(
            draft_room_module,
            "promote_input",
            new=AsyncMock(side_effect=exc),
        ):
            resp = self._promote(draft_id, source_type="input", source_id=input_id)
        return draft_id, resp

    def test_each_typed_branch_with_cleanup_incomplete_logs_one_error(self):
        for label, exc_cls, expected_status, expected_code in _TYPED_BRANCHES:
            with self.subTest(branch=label):
                exc = exc_cls(_POISON_MESSAGE)
                with self.assertLogs(ROUTE_LOGGER, level="ERROR") as captured:
                    draft_id, resp = self._promote_with_typed_error(exc)
                self.assertEqual(
                    resp.status_code,
                    expected_status,
                    f"the {label} branch must still answer {expected_status} "
                    f"({expected_code}); got {resp.status_code}: {resp.text}",
                )
                self.assertEqual(
                    resp.json().get("code"),
                    expected_code,
                    f"the {label} branch response code must stay "
                    f"{expected_code}; got {resp.text}",
                )
                errors = [
                    r for r in captured.records if r.levelno >= logging.ERROR
                ]
                self.assertEqual(
                    len(errors),
                    1,
                    f"typed_cleanup_error_record: exactly one ERROR record is "
                    f"expected for the {label} branch with cleanup_incomplete="
                    f"True; got {[r.getMessage() for r in errors]}",
                )
                message = errors[0].getMessage()
                self.assertIn(
                    str(draft_id),
                    message,
                    "typed_cleanup_error_record: the ERROR record must carry "
                    "the draft_id",
                )
                self.assertIn(
                    type(exc).__name__,
                    message,
                    "typed_cleanup_error_record: the ERROR record must carry "
                    "the error class name",
                )
                self.assertNotIn(
                    _POISON_MESSAGE,
                    message,
                    "typed_cleanup_error_record: the ERROR record must not "
                    "carry exception message text (#789 leak class)",
                )
                # Plan-critic round 1: the exact shipped format is pinned —
                # draft_id + class name are the ONLY dynamic values.
                self.assertRegex(
                    message,
                    rf"^draft_room: promotion cleanup incomplete "
                    rf"draft_id={draft_id} error={type(exc).__name__}$",
                    "typed_cleanup_error_record: the ERROR record must match "
                    "the exact shipped format 'draft_room: promotion cleanup "
                    "incomplete draft_id=<id> error=<Class>' with no other "
                    "dynamic text",
                )


class TestGenericBranchKeepsExactlyItsOwnError(DraftPromoteTestBase):
    """AC10/AC11 boundary (plan-critic round 1, wording corrected round 2):
    the GENERIC branch keeps exactly its own SHIPPED TWO error-channel
    records — its ``logger.exception`` plus the module 5xx response
    handler's log (issue #789: every 500 is logged with cause).

    The typed-branch fix must add neither a third record nor the typed
    cleanup-failure line on the generic branch (#916's explicit exclusion:
    the generic branch's log already exists). GREEN at both ends — a
    preservation pin against the fix's placement."""

    longMessage = False

    def test_generic_branch_cleanup_incomplete_logs_exactly_one_error(self):
        from app.services.draft_promotion import DraftPromotionError

        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        exc = DraftPromotionError(_POISON_MESSAGE)
        exc.cleanup_incomplete = True
        with patch.object(
            draft_room_module,
            "promote_input",
            new=AsyncMock(side_effect=exc),
        ):
            with self.assertLogs(ROUTE_LOGGER, level="ERROR") as captured:
                resp = self._promote(
                    draft_id, source_type="input", source_id=input_id
                )
        self.assertEqual(resp.status_code, 500)
        errors = [r for r in captured.records if r.levelno >= logging.ERROR]
        # The generic branch's SHIPPED baseline is exactly TWO error-channel
        # records: its own ``logger.exception("... promotion failed
        # draft_id=...")`` plus the module 5xx handler's response log
        # (issue #789: every 500 is logged with cause before rendering).
        # The typed-branch fix must add NEITHER a third record nor the
        # typed cleanup-failure line here.
        self.assertEqual(
            len(errors),
            2,
            "generic_branch_preserved: the generic branch must keep exactly "
            "its own two baseline ERROR records (logger.exception + the 5xx "
            f"response handler); got {[r.getMessage() for r in errors]}",
        )
        self.assertTrue(
            any("promotion failed draft_id=" in r.getMessage() for r in errors),
            "generic_branch_preserved: the generic branch's own "
            "logger.exception record must remain",
        )
        for record in errors:
            self.assertNotIn(
                "promotion cleanup incomplete",
                record.getMessage(),
                "generic_branch_preserved: the typed-branch cleanup-failure "
                "line must NOT fire on the generic branch (#916 exclusion)",
            )


class TestTypedCleanupCompleteBranchesStayUnlogged(DraftPromoteTestBase):
    """AC11 negative arm: cleanup-complete typed branches stay unlogged.

    An ordinary clean typed refusal (``cleanup_incomplete=False``) is normal
    client traffic, not a compensation failure, and must stay out of the
    error channel. GREEN at both ends; the module's RED at base comes from
    the positive arm above."""

    longMessage = False

    def _ready_draft_with_input(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return draft_id, input_id

    def test_typed_branches_with_cleanup_complete_stay_out_of_the_error_channel(
        self,
    ):
        for label, exc_cls, expected_status, expected_code in _TYPED_BRANCHES:
            with self.subTest(branch=label):
                draft_id, input_id = self._ready_draft_with_input()
                exc = exc_cls(_POISON_MESSAGE)
                self.assertFalse(exc.cleanup_incomplete)
                with patch.object(
                    draft_room_module,
                    "promote_input",
                    new=AsyncMock(side_effect=exc),
                ):
                    with self.assertNoLogs(ROUTE_LOGGER, level="ERROR"):
                        resp = self._promote(
                            draft_id, source_type="input", source_id=input_id
                        )
                self.assertEqual(
                    resp.status_code,
                    expected_status,
                    f"the {label} branch must still answer {expected_status} "
                    f"when cleanup completed; got {resp.status_code}: "
                    f"{resp.text}",
                )
                self.assertEqual(
                    resp.json().get("code"),
                    expected_code,
                    f"the {label} branch response code must stay "
                    f"{expected_code} when cleanup completed; got {resp.text}",
                )


if __name__ == "__main__":
    unittest.main()
