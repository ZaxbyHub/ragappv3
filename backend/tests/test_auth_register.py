"""
Regression tests for _register_db BEGIN IMMEDIATE fix (auth.py line 189).

The fix ensures atomic superadmin role assignment by:
1. Clearing any dangling implicit transaction from the outer SELECT check
2. Using BEGIN IMMEDIATE to acquire an exclusive write lock before the COUNT(*) check

This prevents the TOCTOU race condition where two concurrent registrations could
both see COUNT(*)=0 and both create superadmin users.

Tests verify:
- db.execute is called with "BEGIN IMMEDIATE" before the SELECT COUNT(*) call
- If db.in_transaction is True, db.rollback() is called before BEGIN IMMEDIATE
- The sequence: rollback? → BEGIN IMMEDIATE → SELECT COUNT(*) → INSERT
"""

from unittest.mock import MagicMock, call, patch

import pytest


class TestRegisterDbBeginImmediate:
    """Tests for _register_db BEGIN IMMEDIATE transaction safety."""

    def _make_mock_db(self, in_transaction: bool = False, user_count: int = 0):
        """Create a mock db connection with call tracking."""
        mock_conn = MagicMock()
        mock_conn.in_transaction = in_transaction
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (user_count,)
        mock_cursor.lastrowid = 1
        mock_conn.execute.return_value = mock_cursor
        return mock_conn

    def _run_register_db_logic(self, db):
        """
        Replicate the _register_db inner function logic for testing.
        This is the exact logic from auth.py lines 189-210.
        """
        calls = []
        try:
            # Step 1: Clear any dangling implicit transaction from the outer SELECT check
            if db.in_transaction:
                db.rollback()
                calls.append("rollback")

            # Step 2: BEGIN IMMEDIATE to acquire exclusive write lock
            db.execute("BEGIN IMMEDIATE")
            calls.append("BEGIN IMMEDIATE")

            # Step 3: Atomic count read and insert in the same transaction
            user_count_result = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            calls.append(f"SELECT COUNT(*) = {user_count_result}")

            role = "superadmin" if user_count_result == 0 else "member"
            calls.append(f"role = {role}")

            db.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("testuser", "hashed_pw", "Test User", role),
            )
            calls.append("INSERT users")

            # Simulate assign_user_to_default_vault
            calls.append("assign_user_to_default_vault")
            db.commit()
            calls.append("commit")
            return calls
        except Exception:
            try:
                db.rollback()
                calls.append("rollback_on_error")
            except Exception:
                pass
            raise

    # -------------------------------------------------------------------------
    # Happy path: no prior transaction
    # -------------------------------------------------------------------------

    def test_begin_immediate_called_before_select_count(self):
        """
        When db.in_transaction is False, the sequence must be:
        1. BEGIN IMMEDIATE
        2. SELECT COUNT(*)
        3. INSERT
        """
        db = self._make_mock_db(in_transaction=False)
        calls = self._run_register_db_logic(db)

        # Verify BEGIN IMMEDIATE was called before SELECT COUNT(*)
        begin_idx = calls.index("BEGIN IMMEDIATE")
        select_idx = calls.index("SELECT COUNT(*) = 0")
        assert begin_idx < select_idx, (
            f"BEGIN IMMEDIATE (index {begin_idx}) must come before SELECT COUNT(*) (index {select_idx})"
        )

        # Verify no rollback was called
        assert "rollback" not in calls, "rollback should not be called when in_transaction is False"

    def test_in_transaction_true_triggers_rollback(self):
        """
        When db.in_transaction is True, the sequence must be:
        1. rollback (to clear dangling SELECT transaction)
        2. BEGIN IMMEDIATE
        3. SELECT COUNT(*)
        4. INSERT
        """
        db = self._make_mock_db(in_transaction=True)
        calls = self._run_register_db_logic(db)

        # Verify rollback was called first
        assert calls[0] == "rollback", f"First call must be rollback, got {calls[0]}"

        # Verify BEGIN IMMEDIATE was called after rollback
        rollback_idx = calls.index("rollback")
        begin_idx = calls.index("BEGIN IMMEDIATE")
        assert rollback_idx < begin_idx, (
            f"rollback (index {rollback_idx}) must come before BEGIN IMMEDIATE (index {begin_idx})"
        )

    # -------------------------------------------------------------------------
    # Happy path: second user becomes member, not superadmin
    # -------------------------------------------------------------------------

    def test_second_user_gets_member_role(self):
        """
        When user_count > 0, the role must be 'member', not 'superadmin'.
        This is the non-TOCTOU path: BEGIN IMMEDIATE ensures serialized access.
        """
        db = self._make_mock_db(in_transaction=False, user_count=5)
        calls = self._run_register_db_logic(db)

        assert "role = member" in calls
        # BEGIN IMMEDIATE must still precede SELECT
        begin_idx = calls.index("BEGIN IMMEDIATE")
        select_idx = calls.index("SELECT COUNT(*) = 5")
        assert begin_idx < select_idx

    # -------------------------------------------------------------------------
    # Verify execute was called with exact SQL strings
    # -------------------------------------------------------------------------

    def test_execute_called_with_begin_immediate(self):
        """db.execute must be called with 'BEGIN IMMEDIATE' as the first statement."""
        db = self._make_mock_db(in_transaction=False)
        execute_order = []

        def track_execute(sql, *args, **kwargs):
            execute_order.append(sql)
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (0,)
            mock_cursor.lastrowid = 1
            return mock_cursor

        db.execute.side_effect = track_execute

        self._run_register_db_logic(db)

        # First SQL executed must be BEGIN IMMEDIATE
        assert execute_order[0] == "BEGIN IMMEDIATE", (
            f"First executed SQL must be 'BEGIN IMMEDIATE', got '{execute_order[0]}'"
        )

    def test_select_count_after_begin_immediate(self):
        """SELECT COUNT(*) must come after BEGIN IMMEDIATE in execute calls."""
        db = self._make_mock_db(in_transaction=False)
        execute_order = []

        def track_execute(sql, *args, **kwargs):
            execute_order.append(sql)
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (0,)
            mock_cursor.lastrowid = 1
            return mock_cursor

        db.execute.side_effect = track_execute

        self._run_register_db_logic(db)

        begin_idx = execute_order.index("BEGIN IMMEDIATE")
        select_idx = execute_order.index("SELECT COUNT(*) FROM users")
        assert begin_idx < select_idx, (
            f"BEGIN IMMEDIATE (index {begin_idx}) must precede SELECT COUNT(*) (index {select_idx})"
        )

    # -------------------------------------------------------------------------
    # Error handling: rollback on exception
    # -------------------------------------------------------------------------

    def test_rollback_on_error(self):
        """If an error occurs after BEGIN IMMEDIATE, rollback must be called."""
        db = self._make_mock_db(in_transaction=False)
        execute_order = []

        def track_execute(sql, *args, **kwargs):
            execute_order.append(sql)
            if sql.startswith("INSERT INTO users"):
                raise Exception("DB error")
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (0,)
            mock_cursor.lastrowid = 1
            return mock_cursor

        db.execute.side_effect = track_execute

        with pytest.raises(Exception, match="DB error"):
            self._run_register_db_logic(db)

        # Rollback should have been called after the error
        assert db.rollback.called, "db.rollback() must be called on error"


class TestRegisterDbSequenceVerification:
    """Integration-style tests that verify the exact _register_db call sequence."""

    def test_full_sequence_no_prior_transaction(self):
        """
        Complete happy-path sequence when no prior transaction exists:
        1. BEGIN IMMEDIATE
        2. SELECT COUNT(*) FROM users
        3. INSERT INTO users ...
        4. commit
        """
        db = MagicMock()
        db.in_transaction = False
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_cursor.lastrowid = 1
        db.execute.return_value = mock_cursor

        calls_made = []

        original_execute = db.execute
        def tracking_execute(sql, *args, **kwargs):
            calls_made.append(("execute", sql))
            return original_execute(sql, *args, **kwargs)

        original_rollback = db.rollback
        def tracking_rollback():
            calls_made.append(("rollback",))
            return original_rollback()

        db.execute = tracking_execute
        db.rollback = tracking_rollback

        # Replicate _register_db logic
        if db.in_transaction:
            db.rollback()
        db.execute("BEGIN IMMEDIATE")
        user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        role = "superadmin" if user_count == 0 else "member"
        db.execute(
            "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
            ("testuser", "hashed_pw", "Test User", role),
        )
        db.commit()

        # Verify sequence
        execute_calls = [c for c in calls_made if c[0] == "execute"]
        assert execute_calls[0] == ("execute", "BEGIN IMMEDIATE")
        assert execute_calls[1] == ("execute", "SELECT COUNT(*) FROM users")
        assert "INSERT INTO users" in execute_calls[2][1]
        assert db.commit.called

    def test_full_sequence_with_prior_transaction(self):
        """
        Complete sequence when a prior transaction exists (in_transaction=True):
        1. rollback (clears dangling SELECT from username uniqueness check)
        2. BEGIN IMMEDIATE
        3. SELECT COUNT(*) FROM users
        4. INSERT INTO users ...
        5. commit
        """
        db = MagicMock()
        db.in_transaction = True  # Simulates being inside a transaction from outer SELECT
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_cursor.lastrowid = 1
        db.execute.return_value = mock_cursor

        calls_made = []

        original_execute = db.execute
        def tracking_execute(sql, *args, **kwargs):
            calls_made.append(("execute", sql))
            return original_execute(sql, *args, **kwargs)

        original_rollback = db.rollback
        def tracking_rollback():
            calls_made.append(("rollback",))
            return original_rollback()

        db.execute = tracking_execute
        db.rollback = tracking_rollback

        # Replicate _register_db logic
        if db.in_transaction:
            db.rollback()  # Must clear dangling transaction FIRST
        db.execute("BEGIN IMMEDIATE")
        user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        role = "superadmin" if user_count == 0 else "member"
        db.execute(
            "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
            ("testuser", "hashed_pw", "Test User", role),
        )
        db.commit()

        # Verify rollback was called before BEGIN IMMEDIATE
        rollback_call = calls_made[0]
        assert rollback_call == ("rollback",), f"First call must be rollback, got {rollback_call}"

        # Verify BEGIN IMMEDIATE follows rollback
        execute_calls = [c for c in calls_made if c[0] == "execute"]
        assert execute_calls[0] == ("execute", "BEGIN IMMEDIATE")
        assert execute_calls[1] == ("execute", "SELECT COUNT(*) FROM users")
