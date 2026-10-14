"""Coverage companion for issue #789's IntegrityError key-race arm.

The frozen module's ``_IdempotencyRaceConnection`` answers EVERY
``(created_by, idempotency_key)`` lookup as a miss while armed, so the
discriminator's replay-RETURN sub-branch (lookup missed, INSERT lost the key
index, post-rollback re-read finds a fingerprint-identical winner → return
it) can never execute under it — only the 409 sub-arm can. This companion
narrows the simulation: the FIRST lookup (the enqueue pre-check) misses and
every later lookup, including the discriminator's post-rollback re-read, sees
the truth. RED at base (the pre-fix handler maps the race to ``active_job``
409), GREEN after the fix (202, the winner's id, no second row).

HONEST SCOPE (issue #789 review F-003). This models a window that SQLite's
single-writer discipline currently makes **unproducible**:
``_sync_enqueue_compile`` takes ``BEGIN IMMEDIATE`` before the pre-check
SELECT and holds the write lock through the INSERT, so no concurrent winner
can commit in between. The race arm is therefore defense-in-depth — it becomes
reachable if the pre-check ever moves outside the transaction, ``BEGIN
DEFERRED`` is adopted, a stale read transaction leaks from the pool, or the
store is not SQLite. The test pins that protection (a regression in the
discriminator fails it); it does not claim the window occurs in production
today.
"""

import sqlite3
import unittest

from app.api.deps import get_db
from app.main import app
from tests.draft_room.test_n05_route_contracts import CompileRouteTestBase


class _FirstLookupOnlyRaceConnection(sqlite3.Connection):
    """Hide exactly the first ``(created_by, idempotency_key)`` lookup.

    ``_IdempotencyRaceConnection`` in the frozen module hides all of them,
    which is right for pinning the 409 arm but makes the replay-return arm
    unsatisfiable. A concurrent winner commits between a request's ONE
    pre-check lookup and its INSERT; once the race is lost, reality is
    readable again — the discriminator's re-query is a fresh statement and
    must see the committed row.
    """

    hidden_so_far = 0
    hide_first_lookup = False

    def execute(self, sql, params=()):  # type: ignore[override]
        if (
            type(self).hide_first_lookup
            and sql.lstrip().upper().startswith("SELECT")
            and "idempotency_key = ?" in sql
        ):
            type(self).hidden_so_far += 1
            if type(self).hidden_so_far == 1:
                return super().execute(
                    "SELECT id, draft_id, input_json FROM draft_jobs WHERE 1 = 0", ()
                )
        return super().execute(sql, params)


class TestN05BRaceReplay(CompileRouteTestBase):
    def test_key_race_with_identical_fingerprint_returns_the_winner(self):
        """A racing IDENTICAL replay that loses the unique-index race still
        gets the original job (202, same id, no second row) — the same
        contract the pre-check replay honors, extended to the race window."""
        draft_id = self._draft_with_ready_input()
        key = "n05b-race-key"

        first = self._compile(draft_id, idempotency_key=key)
        self.assertEqual(first.status_code, 202, first.text)
        winner_id = first.json()["id"]

        # Make the winner non-active (so the active-job guard cannot fire
        # before the INSERT) and the draft compile-eligible again, exactly as
        # the raced window would look: the pre-check lookup is the ONLY
        # statement that must miss.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_jobs SET status = 'completed' WHERE id = ?",
                (winner_id,),
            )
            conn.execute(
                "UPDATE drafts SET status = 'needs_review' WHERE id = ?",
                (draft_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        lv = self._lock_version(draft_id)

        original_override = app.dependency_overrides.get(get_db)

        def _racing_get_db():
            racing_conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=10,
                factory=_FirstLookupOnlyRaceConnection,
            )
            racing_conn.row_factory = sqlite3.Row
            racing_conn.execute("PRAGMA foreign_keys = ON;")
            try:
                yield racing_conn
            finally:
                racing_conn.close()

        app.dependency_overrides[get_db] = _racing_get_db
        _FirstLookupOnlyRaceConnection.hide_first_lookup = True
        _FirstLookupOnlyRaceConnection.hidden_so_far = 0
        try:
            resp = self.client.post(
                f"/api/draft-room/drafts/{draft_id}/compile",
                json={
                    "base_revision_id": None,
                    "lock_version": lv,
                    "start_stage": "research",
                },
                headers={**self._owner_headers(), "Idempotency-Key": key},
            )
        finally:
            engaged = _FirstLookupOnlyRaceConnection.hidden_so_far
            _FirstLookupOnlyRaceConnection.hide_first_lookup = False
            _FirstLookupOnlyRaceConnection.hidden_so_far = 0
            if original_override is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = original_override

        # Issue #789 review F-004: this test proves the same outcome through the
        # ORDINARY pre-check replay path when the fault injection silently stops
        # matching (any edit to the production lookup's SQL text, spacing, or
        # parameter style). Assert the injection actually engaged — at least
        # the hidden pre-check AND the discriminator's re-read — so future SQL
        # drift fails loudly instead of leaving this green-but-vacuous.
        self.assertGreaterEqual(
            engaged,
            2,
            "the race simulation never engaged: the production idempotency "
            "lookup's SQL text drifted, so this test silently fell back to the "
            "ordinary replay path",
        )

        self.assertEqual(resp.status_code, 202, resp.text)
        self.assertEqual(
            resp.json()["id"],
            winner_id,
            "a racing identical replay must be answered with the winner, "
            "not a conflict and not a second job",
        )

        conn = self._connection_pool.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'compile'",
                (draft_id,),
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            self._connection_pool.release_connection(conn)


if __name__ == "__main__":
    unittest.main()
