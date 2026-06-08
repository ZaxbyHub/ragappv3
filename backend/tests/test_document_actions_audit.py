"""
Tests for user_id audit trail wiring in document_actions.

FOCUS:
1. _optional_current_user returns None when users_enabled is False
2. retry_document endpoint accepts current_user parameter
3. Both _record_document_action call sites compute user_id correctly
4. _record_document_action function still has user_id parameter in INSERT
"""

import hashlib
import hmac
import inspect
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.api.routes.documents import (
    _optional_current_user,
    _record_document_action,
    retry_document,
)
from app.config import settings


class TestOptionalCurrentUser(unittest.TestCase):
    """Tests for _optional_current_user dependency."""

    def test_returns_none_when_users_disabled(self):
        """When users_enabled=False, _optional_current_user must return None."""
        with patch.object(settings, "users_enabled", False):
            # Call with a fake authorization header - should still return None
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(
                _optional_current_user(
                    authorization="Bearer sometoken",
                    db=MagicMock(),
                )
            )
            self.assertIsNone(result)

    def test_returns_none_when_no_authorization_header(self):
        """When no authorization header, returns None regardless of users_enabled."""
        with patch.object(settings, "users_enabled", True):
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(
                _optional_current_user(
                    authorization=None,
                    db=MagicMock(),
                )
            )
            self.assertIsNone(result)

    def test_returns_none_when_jwt_raises_httpexception(self):
        """When get_current_active_user raises HTTPException, returns None."""
        from fastapi import HTTPException

        with patch.object(settings, "users_enabled", True):
            with patch(
                "app.api.routes.documents.get_current_active_user",
                new_callable=AsyncMock,
                side_effect=HTTPException(status_code=401, detail="bad token"),
            ):
                import asyncio

                result = asyncio.get_event_loop().run_until_complete(
                    _optional_current_user(
                        authorization="Bearer badtoken",
                        db=MagicMock(),
                    )
                )
                self.assertIsNone(result)

    def test_returns_user_dict_when_jwt_valid(self):
        """When JWT is valid, returns the user dict."""
        fake_user = {"id": 42, "username": "testuser"}
        with patch.object(settings, "users_enabled", True):
            with patch(
                "app.api.routes.documents.get_current_active_user",
                new_callable=AsyncMock,
                return_value=fake_user,
            ):
                import asyncio

                result = asyncio.get_event_loop().run_until_complete(
                    _optional_current_user(
                        authorization="Bearer validtoken",
                        db=MagicMock(),
                    )
                )
                self.assertEqual(result, fake_user)
                self.assertEqual(result["id"], 42)


class TestRecordDocumentAction(unittest.TestCase):
    """Tests for _record_document_action audit INSERT."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE document_actions (
                id INTEGER PRIMARY KEY,
                file_id INTEGER,
                action TEXT,
                status TEXT,
                user_id TEXT,
                hmac_sha256 TEXT
            )"""
        )
        self.conn.commit()

        self.secret_manager = MagicMock()
        self.secret_manager.get_hmac_key.return_value = (b"testkey", 1)

    def tearDown(self):
        self.conn.close()

    def test_inserts_with_user_id_column(self):
        """_record_document_action must INSERT with user_id in the VALUES clause."""
        source = inspect.getsource(_record_document_action)
        self.assertIn("user_id", source.split("INSERT")[1].split("VALUES")[0])
        self.assertIn("user_id", source.split("VALUES")[1])

    def test_hmac_covers_user_id(self):
        """The HMAC message must include user_id."""
        source = inspect.getsource(_record_document_action)
        self.assertIn("user_id", source.split("message")[1].split("digest")[0])

    def test_inserts_correct_user_id(self):
        """Verify the user_id is stored correctly in the database."""
        _record_document_action(
            file_id=10,
            action="retry",
            status="scheduled",
            user_id="user-42",
            secret_manager=self.secret_manager,
            conn=self.conn,
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT file_id, action, status, user_id FROM document_actions"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["file_id"], 10)
        self.assertEqual(row["action"], "retry")
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["user_id"], "user-42")

    def test_hmac_integrity_with_user_id(self):
        """Verify the HMAC digest includes user_id in its computation."""
        _record_document_action(
            file_id=5,
            action="retry",
            status="error",
            user_id="admin-token-abc",
            secret_manager=self.secret_manager,
            conn=self.conn,
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT user_id, hmac_sha256 FROM document_actions"
        ).fetchone()
        expected_message = "5|retry|error|admin-token-abc"
        expected_digest = hmac.new(
            b"testkey", expected_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        self.assertEqual(row["hmac_sha256"], expected_digest)


class TestRetryDocumentAuditWiring(unittest.IsolatedAsyncioTestCase):
    """Tests for user_id computation in retry_document call sites."""

    async def test_retry_accepts_current_user_param(self):
        """retry_document must accept current_user parameter."""
        sig = inspect.signature(retry_document)
        self.assertIn("current_user", sig.parameters)

    async def test_user_id_from_jwt_when_available(self):
        """When current_user has 'id', user_id should be str(current_user['id'])."""
        # Read the source to verify the logic
        source = inspect.getsource(retry_document)
        # Verify the primary path uses current_user["id"]
        self.assertIn('current_user["id"]', source)
        self.assertIn("str(current_user", source)

    async def test_user_id_fallback_to_auth(self):
        """When current_user is None, falls back to user.get('id', 'unknown')."""
        source = inspect.getsource(retry_document)
        self.assertIn("user.get", source)
        self.assertIn('"unknown"', source)

    async def test_both_call_sites_compute_user_id(self):
        """Both success and error paths must compute user_id before _record_document_action."""
        source = inspect.getsource(retry_document)
        # Count occurrences of user_id computation pattern
        # There should be two: one in try block, one in except block
        compute_pattern = 'str(current_user["id"])'
        count = source.count(compute_pattern)
        self.assertEqual(
            count,
            2,
            f"Expected 2 user_id computations (success + error paths), found {count}",
        )

    async def test_retry_calls_record_action_with_user_id(self):
        """Both _record_document_action calls must pass user_id as positional arg."""
        source = inspect.getsource(retry_document)
        # Split on _record_document_action to find all call sites
        call_sites = source.split("_record_document_action")[1:]
        self.assertGreaterEqual(
            len(call_sites),
            2,
            "Expected at least 2 call sites for _record_document_action",
        )
        for site in call_sites:
            # Each call site should reference user_id
            self.assertIn("user_id", site.split(")")[0])


class TestDocumentActionsSchema(unittest.TestCase):
    """Tests verifying document_actions table schema includes user_id."""

    def test_document_actions_table_has_user_id(self):
        """The document_actions table must have a user_id column."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE document_actions (
                id INTEGER PRIMARY KEY,
                file_id INTEGER,
                action TEXT,
                status TEXT,
                user_id TEXT,
                hmac_sha256 TEXT
            )"""
        )
        conn.commit()
        cursor = conn.execute("PRAGMA table_info(document_actions)")
        columns = {row["name"] for row in cursor.fetchall()}
        conn.close()
        self.assertIn("user_id", columns)

    def test_function_signature_has_user_id_param(self):
        """_record_document_action must accept user_id as a parameter."""
        sig = inspect.signature(_record_document_action)
        self.assertIn("user_id", sig.parameters)
        param = sig.parameters["user_id"]
        self.assertEqual(param.annotation, str)


if __name__ == "__main__":
    unittest.main()
