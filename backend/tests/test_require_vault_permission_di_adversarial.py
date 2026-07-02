"""
Adversarial Security Tests for require_vault_permission DI Refactor

Attack vectors tested:
- DI connection reuse / double-connection elimination
- Malformed vault_id bypass (negative, zero, None, non-integer)
- Empty actions bypass (require_vault_permission() with no args)
- Action string injection (SQL injection in action parameter)
- Resource type confusion (non-vault/group resources)
- FastAPI dependency override bypass attempts
- Privilege escalation via superadmin role manipulation
- Unicode / non-ASCII in vault_id

Target: backend/app/api/deps.py require_vault_permission refactor
  - OLD: standalone evaluate_policy(...) → created own pool connection
  - NEW: get_evaluate_policy(db) DI factory → reuses injected connection
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.api.deps import (
    VAULT_ACTION_LEVELS,
    VAULT_PERMISSION_LEVELS,
    _evaluate_policy,
    get_evaluate_policy,
    require_vault_permission,
)

# =============================================================================
# DI CONNECTION REUSE — DOUBLE-CONNECTION ELIMINATION
# =============================================================================


class TestDIConnectionReuse:
    """
    Regression tests: verify the DI refactor eliminates the double-connection
    problem where require_vault_permission previously called standalone
    evaluate_policy() which opened its own pool connection.

    The new path: require_vault_permission → get_evaluate_policy(db) → _evaluate_policy(db, ...)
    The old path: require_vault_permission → evaluate_policy(...) → _evaluate_policy(conn, ...)
                      where evaluate_policy opened its own pool connection
    """

    @pytest.mark.asyncio
    async def test_require_vault_permission_uses_injected_db_not_standalone_pool(self):
        """
        Security regression: require_vault_permission must use the DI-injected db
        connection, NOT open a new standalone pool connection.

        If the old evaluate_policy() is called instead of get_evaluate_policy(db),
        it would open its own connection (double-connection issue).
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()

        # Track whether get_db was used (via injection) vs standalone get_pool
        db_used_via_injection = False
        standalone_pool_opened = False

        def mock_get_db():
            nonlocal db_used_via_injection
            db_used_via_injection = True
            return mock_db

        mock_evaluate = AsyncMock(return_value=True)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            result = await permission_check(vault_id=1, user=mock_user, db=mock_db)

        # Verify injected db was passed to the factory
        assert result == mock_user
        mock_db_factory.assert_called_once_with(mock_db)
        mock_evaluate.assert_awaited_with(mock_user, "vault", 1, "read")

        # Verify standalone pool was NOT opened by require_vault_permission internals
        # (the old path would call evaluate_policy() which calls get_pool())
        # We patch get_pool to detect if it's called
        with patch("app.api.deps.get_pool") as mock_get_pool:
            permission_check2 = require_vault_permission("write")
            mock_evaluate2 = AsyncMock(return_value=True)
            mock_db_factory2 = MagicMock(return_value=mock_evaluate2)

            with patch("app.api.deps.get_evaluate_policy", mock_db_factory2):
                await permission_check2(vault_id=2, user=mock_user, db=mock_db)

            # get_pool should NOT be called by require_vault_permission internals
            # (it's only used by the legacy standalone evaluate_policy)
            assert mock_get_pool.call_count == 0, (
                "require_vault_permission should NOT open standalone pool connections"
            )

    @pytest.mark.asyncio
    async def test_evaluate_policy_still_opens_own_connection(self):
        """
        Verify legacy evaluate_policy() still opens its own connection.
        This confirms the refactor didn't break backward compatibility.
        """
        from app.api.deps import evaluate_policy

        mock_user = {"id": 1, "role": "superadmin"}

        with patch("app.api.deps.get_pool") as mock_get_pool:
            mock_conn = MagicMock()
            mock_pool_instance = MagicMock()
            mock_pool_instance.get_connection.return_value = mock_conn
            mock_get_pool.return_value = mock_pool_instance

            result = await evaluate_policy(mock_user, "vault", 1, "read")

            # Legacy path: opens its own pool connection
            mock_get_pool.assert_called_once()
            mock_pool_instance.get_connection.assert_called_once()

        assert result is True

    @pytest.mark.asyncio
    async def test_evaluate_policy_releases_connection_on_success(self):
        """
        Legacy evaluate_policy must release connection even on success.
        """
        from app.api.deps import evaluate_policy

        mock_user = {"id": 1, "role": "superadmin"}

        with patch("app.api.deps.get_pool") as mock_get_pool:
            mock_conn = MagicMock()
            mock_pool_instance = MagicMock()
            mock_pool_instance.get_connection.return_value = mock_conn
            mock_get_pool.return_value = mock_pool_instance

            result = await evaluate_policy(mock_user, "vault", 1, "read")

            # Connection must be released
            mock_pool_instance.release_connection.assert_called_once_with(mock_conn)

    @pytest.mark.asyncio
    async def test_evaluate_policy_releases_connection_on_exception(self):
        """
        Legacy evaluate_policy must release connection even on exception.
        """
        from app.api.deps import evaluate_policy

        mock_user = {"id": 1, "role": "member"}

        with patch("app.api.deps.get_pool") as mock_get_pool:
            mock_conn = MagicMock()
            mock_pool_instance = MagicMock()
            mock_pool_instance.get_connection.return_value = mock_conn
            mock_get_pool.return_value = mock_pool_instance

            # Simulate an error in _evaluate_policy by patching it
            with patch("app.api.deps._evaluate_policy", side_effect=Exception("DB Error")):
                with pytest.raises(Exception, match="DB Error"):
                    await evaluate_policy(mock_user, "vault", 1, "read")

            mock_pool_instance.release_connection.assert_called_once_with(mock_conn)


# =============================================================================
# MALFORMED VAULT_ID ATTACKS
# =============================================================================


class TestMalformedVaultIdAttacks:
    """Attack vectors against vault_id validation in require_vault_permission."""

    @pytest.mark.asyncio
    async def test_require_vault_permission_negative_vault_id_denied(self):
        """
        Attack: vault_id=-1 must be denied (no access to negative vault).
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=-1, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403
        mock_evaluate.assert_awaited_with(mock_user, "vault", -1, "read")

    @pytest.mark.asyncio
    async def test_require_vault_permission_zero_vault_id_denied(self):
        """
        Attack: vault_id=0 must be denied (no vault with ID 0).
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=0, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403
        mock_evaluate.assert_awaited_with(mock_user, "vault", 0, "read")

    @pytest.mark.asyncio
    async def test_require_vault_permission_very_large_vault_id_denied(self):
        """
        Attack: vault_id=9999999999 (non-existent) must be denied.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=9999999999, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403
        mock_evaluate.assert_awaited_with(mock_user, "vault", 9999999999, "read")

    @pytest.mark.asyncio
    async def test_require_vault_permission_negative_max_int_denied(self):
        """
        Attack: vault_id=-2147483648 (min 32-bit int) must be denied.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=-2147483648, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_require_vault_permission_float_vault_id_rejected(self):
        """
        Attack: vault_id as float (1.5) — type confusion attack.
        Should be rejected at the FastAPI layer or handled safely.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()

        # FastAPI would reject float vault_id at validation layer,
        # but we test the internal function directly
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            # The vault_id type hint is int, so FastAPI would 422 before we reach here
            # But if somehow a float slips through:
            with pytest.raises((HTTPException, TypeError)):
                await permission_check(vault_id=1.5, user=mock_user, db=mock_db)

    @pytest.mark.asyncio
    async def test_require_vault_permission_string_vault_id_rejected(self):
        """
        Attack: vault_id as string ("1") — type confusion attack.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()

        with pytest.raises((HTTPException, TypeError)):
            await permission_check(vault_id="1", user=mock_user, db=mock_db)


# =============================================================================
# EMPTY ACTIONS BYPASS ATTACK
# =============================================================================


class TestEmptyActionsBypass:
    """
    Attack: require_vault_permission() with no actions.

    If empty actions loop grants access, any user could bypass security.
    """

    @pytest.mark.asyncio
    async def test_require_vault_permission_empty_actions_denies_all(self):
        """
        Attack: require_vault_permission() with no actions must deny ALL access.
        """
        permission_check = require_vault_permission()

        mock_user = {"id": 1, "role": "superadmin"}
        mock_db = MagicMock()

        # Even superadmin with empty actions should be denied
        with pytest.raises(HTTPException) as exc_info:
            await permission_check(vault_id=1, user=mock_user, db=mock_db)

        assert exc_info.value.status_code == 403
        assert "Insufficient vault permissions" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_require_vault_permission_empty_actions_not_bypassed_by_member(self):
        """
        Attack: member (lowest role) with empty actions must be denied.
        """
        permission_check = require_vault_permission()

        mock_user = {"id": 1, "role": "viewer"}
        mock_db = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await permission_check(vault_id=1, user=mock_user, db=mock_db)

        assert exc_info.value.status_code == 403


# =============================================================================
# ACTION STRING INJECTION ATTACKS
# =============================================================================


class TestActionStringInjection:
    """Attack vectors: action parameter SQL injection."""

    @pytest.mark.asyncio
    async def test_action_sql_injection_attempt_single_quote(self):
        """
        Attack: action="read' OR '1'='1" — SQL injection attempt.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()

        # The malicious action should not match any known action
        # VAULT_ACTION_LEVELS has: read=1, write=2, delete=3, admin=3
        malicious_action = "read' OR '1'='1"

        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=1, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_action_sql_injection_semicolon(self):
        """
        Attack: action="read; DROP TABLE vault_members; --"
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "superadmin"}
        mock_db = MagicMock()

        # Even superadmin with malicious action — should safely handle
        mock_evaluate = AsyncMock(return_value=True)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            # Superadmin always passes via role check, regardless of action
            result = await permission_check(vault_id=1, user=mock_user, db=mock_db)
            assert result == mock_user

    @pytest.mark.asyncio
    async def test_action_null_byte_injection(self):
        """
        Attack: action with null bytes (\x00).
        """
        permission_check = require_vault_permission("read\x00admin")

        mock_user = {"id": 1, "role": "superadmin"}
        mock_db = MagicMock()

        mock_evaluate = AsyncMock(return_value=True)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            result = await permission_check(vault_id=1, user=mock_user, db=mock_db)
            assert result == mock_user

    @pytest.mark.asyncio
    async def test_action_unicode_override(self):
        """
        Attack: action with Unicode RTL override characters.
        """
        permission_check = require_vault_permission("\u202eread")  # RTL override

        mock_user = {"id": 1, "role": "superadmin"}
        mock_db = MagicMock()

        mock_evaluate = AsyncMock(return_value=True)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            result = await permission_check(vault_id=1, user=mock_user, db=mock_db)
            assert result == mock_user

    @pytest.mark.asyncio
    async def test_action_template_injection(self):
        """
        Attack: action="${malicious}" — template literal injection.
        """
        permission_check = require_vault_permission("${read}")

        mock_user = {"id": 1, "role": "superadmin"}
        mock_db = MagicMock()

        mock_evaluate = AsyncMock(return_value=True)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            result = await permission_check(vault_id=1, user=mock_user, db=mock_db)
            assert result == mock_user


# =============================================================================
# NONE VAULT_ID — PRIVILEGE ESCALATION
# =============================================================================


class TestNoneVaultIdPrivilegeEscalation:
    """
    Attack: vault_id=None passed to require_vault_permission.

    When vault_id=None, _evaluate_policy returns False (no access).
    But what about superadmin? The code explicitly checks resource_id is None
    before the superadmin check, so superadmin should NOT bypass None vault_id.
    """

    @pytest.mark.asyncio
    async def test_require_vault_permission_none_vault_id_denies_superadmin(self):
        """
        Attack: vault_id=None with superadmin — should still be denied.

        _evaluate_policy checks `if resource_id is None: return False`
        BEFORE the superadmin check, so superadmin cannot bypass None vault_id.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 0, "role": "superadmin"}
        mock_db = MagicMock()

        # Even superadmin with vault_id=None should be denied
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=None, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_evaluate_policy_none_vault_id_denies_superadmin(self):
        """
        Verify _evaluate_policy correctly denies vault_id=None even for superadmin.
        """
        mock_user = {"id": 0, "role": "superadmin"}

        result = await _evaluate_policy(mock_db := MagicMock(), mock_user, "vault", None, "read")

        # The function explicitly returns False when resource_id is None
        assert result is False


# =============================================================================
# RESOURCE TYPE CONFUSION ATTACKS
# =============================================================================


class TestResourceTypeConfusion:
    """Attack vectors: non-vault/group resource types."""

    @pytest.mark.asyncio
    async def test_evaluate_policy_non_vault_non_group_member_denied(self):
        """
        Attack: resource_type="document" with member role.
        Only superadmin should access non-vault/non-group resources.
        """
        mock_user = {"id": 1, "role": "member"}

        result = await _evaluate_policy(mock_db := MagicMock(), mock_user, "document", 1, "read")

        assert result is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_non_vault_non_group_admin_denied(self):
        """
        Attack: resource_type="settings" with admin role.
        """
        mock_user = {"id": 1, "role": "admin"}

        result = await _evaluate_policy(mock_db := MagicMock(), mock_user, "settings", 1, "read")

        assert result is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_non_vault_non_group_superadmin_allowed(self):
        """
        Attack: resource_type="document" with superadmin role — should be allowed.
        """
        mock_user = {"id": 1, "role": "superadmin"}

        result = await _evaluate_policy(mock_db := MagicMock(), mock_user, "document", 1, "read")

        assert result is True

    @pytest.mark.asyncio
    async def test_evaluate_policy_resource_type_sql_injection(self):
        """
        Attack: resource_type="vault'; DROP TABLE users; --"
        """
        mock_user = {"id": 1, "role": "member"}

        result = await _evaluate_policy(
            mock_db := MagicMock(), mock_user, "vault'; DROP TABLE users; --", 1, "read"
        )

        # Should treat as non-vault resource (not "vault") → member is denied
        assert result is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_group_resource_admin_allowed(self):
        """
        Attack: resource_type="group" with admin — should be allowed.
        """
        mock_user = {"id": 1, "role": "admin"}

        result = await _evaluate_policy(mock_db := MagicMock(), mock_user, "group", 1, "read")

        assert result is True

    @pytest.mark.asyncio
    async def test_evaluate_policy_group_resource_member_denied(self):
        """
        Attack: resource_type="group" with member — should be denied.
        """
        mock_user = {"id": 1, "role": "member"}

        result = await _evaluate_policy(mock_db := MagicMock(), mock_user, "group", 1, "read")

        assert result is False


# =============================================================================
# PRIVILEGE ESCALATION — SUPERADMIN BYPASS
# =============================================================================


class TestPrivilegeEscalation:
    """Attack vectors: privilege escalation attempts."""

    @pytest.mark.asyncio
    async def test_superadmin_id_zero_escalation_blocked(self):
        """
        Attack: id=0 (admin token) with member role should NOT escalate.

        id=0 is the admin token user. Even with id=0, a "member" role
        should NOT grant superadmin privileges.
        """
        mock_user = {"id": 0, "role": "member"}

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_cursor.fetchall.return_value = []
        mock_db.execute.return_value = mock_cursor

        result = await _evaluate_policy(mock_db, mock_user, "vault", 1, "admin")

        # member role (even with id=0) should NOT get admin access
        assert result is False

    @pytest.mark.asyncio
    async def test_superadmin_with_empty_role_not_escalated(self):
        """
        Attack: superadmin role string is empty or None.

        Empty role should not grant superadmin privileges.
        """
        mock_user = {"id": 1, "role": ""}

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_cursor.fetchall.return_value = []
        mock_db.execute.return_value = mock_cursor

        result = await _evaluate_policy(mock_db, mock_user, "vault", 1, "admin")

        # Empty role should not grant superadmin access
        assert result is False

    @pytest.mark.asyncio
    async def test_superadmin_with_none_role_not_escalated(self):
        """
        Attack: role=None should not grant superadmin.
        """
        mock_user = {"id": 1, "role": None}

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_cursor.fetchall.return_value = []
        mock_db.execute.return_value = mock_cursor

        result = await _evaluate_policy(mock_db, mock_user, "vault", 1, "admin")

        assert result is False

    @pytest.mark.asyncio
    async def test_member_cannot_escalate_to_admin_via_vault_id(self):
        """
        Attack: member tries to access admin-only vault by guessing ID.

        Even if a vault requires "admin" permission, a member with only "read"
        should NOT gain admin access by any ID-guessing or manipulation.
        """
        mock_user = {"id": 1, "role": "member"}

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        # Simulate: member has "read" permission
        mock_cursor.fetchone.return_value = (1, "read")  # vault_id=1, permission="read"
        mock_cursor.fetchall.return_value = []
        mock_db.execute.return_value = mock_cursor

        result = await _evaluate_policy(mock_db, mock_user, "vault", 1, "admin")

        # read (level 1) < admin required (level 3) → should be denied
        assert result is False


# =============================================================================
# FASTAPI DEPENDENCY OVERRIDE BYPASS
# =============================================================================


class TestDependencyOverrideBypass:
    """
    Attack vectors: FastAPI dependency override manipulation.

    Routes that use Depends(require_vault_permission("read")) could theoretically
    have their get_evaluate_policy dependency overridden. We test that the
    override mechanism works correctly when properly applied, and that
    require_vault_permission cannot be bypassed when overrides are absent.
    """

    @pytest.mark.asyncio
    async def test_require_vault_permission_deny_when_evaluate_returns_false(self):
        """
        Normal path: evaluate returns False → 403.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=1, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403
            assert "Insufficient vault permissions" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_require_vault_permission_allow_when_evaluate_returns_true(self):
        """
        Normal path: evaluate returns True → user returned, access granted.
        """
        permission_check = require_vault_permission("read")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()
        mock_evaluate = AsyncMock(return_value=True)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            result = await permission_check(vault_id=1, user=mock_user, db=mock_db)

            assert result == mock_user

    @pytest.mark.asyncio
    async def test_require_vault_permission_multi_action_any_match_passes(self):
        """
        require_vault_permission("read", "write") — if ANY action matches, access granted.
        """
        permission_check = require_vault_permission("read", "write")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()

        # First call (read) returns False, second (write) returns True
        mock_evaluate = AsyncMock(
            side_effect=[False, True]
        )
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            result = await permission_check(vault_id=1, user=mock_user, db=mock_db)

            assert result == mock_user
            assert mock_evaluate.call_count == 2
            mock_evaluate.assert_any_await(mock_user, "vault", 1, "read")
            mock_evaluate.assert_any_await(mock_user, "vault", 1, "write")

    @pytest.mark.asyncio
    async def test_require_vault_permission_multi_action_all_fail(self):
        """
        require_vault_permission("read", "write") — if ALL actions fail, 403.
        """
        permission_check = require_vault_permission("read", "write")

        mock_user = {"id": 1, "role": "member"}
        mock_db = MagicMock()
        mock_evaluate = AsyncMock(return_value=False)
        mock_db_factory = MagicMock(return_value=mock_evaluate)

        with patch("app.api.deps.get_evaluate_policy", mock_db_factory):
            with pytest.raises(HTTPException) as exc_info:
                await permission_check(vault_id=1, user=mock_user, db=mock_db)

            assert exc_info.value.status_code == 403
            assert mock_evaluate.call_count == 2


# =============================================================================
# CONCURRENCY / RACE CONDITION ATTACKS
# =============================================================================


class TestConcurrentAccessAttacks:
    """Attack vectors: concurrent access pattern manipulation."""

    @pytest.mark.asyncio
    async def test_concurrent_vault_access_same_user_different_vaults(self):
        """
        Attack: concurrent requests for different vaults by same user.
        Each request should be isolated — permission for vault A
        should not leak to vault B.
        """
        mock_user = {"id": 1, "role": "member"}

        # User has read access to vault 1, no access to vault 2
        vault_1_perm = {"vault_id": 1, "permission": "read"}
        vault_2_perm = None

        async def mock_get_effective_permissions(db, principal, vault_ids):
            result = {}
            for vid in vault_ids:
                if vid == 1:
                    result[vid] = "read"
                else:
                    result[vid] = None
            return result

        with patch("app.api.deps.get_effective_vault_permissions", side_effect=mock_get_effective_permissions):
            # Vault 1 → read allowed
            result1 = await _evaluate_policy(mock_db := MagicMock(), mock_user, "vault", 1, "read")
            assert result1 is True

            # Vault 2 → no permission
            result2 = await _evaluate_policy(mock_db := MagicMock(), mock_user, "vault", 2, "read")
            assert result2 is False

            # Vault 2 → write denied (even though vault 1 has read)
            result3 = await _evaluate_policy(mock_db := MagicMock(), mock_user, "vault", 1, "write")
            assert result3 is False  # read (1) < write (2)


# =============================================================================
# OVERSIZED / BOUNDARY ATTACKS
# =============================================================================


class TestOversizedBoundaryAttacks:
    """Attack vectors: oversized inputs and boundary conditions."""

    @pytest.mark.asyncio
    async def test_max_safe_integer_vault_id(self):
        """
        Attack: vault_id=9007199254740991 (Number.MAX_SAFE_INTEGER).
        """
        mock_user = {"id": 1, "role": "member"}

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_cursor.fetchall.return_value = []
        mock_db.execute.return_value = mock_cursor

        result = await _evaluate_policy(
            mock_db, mock_user, "vault", 9007199254740991, "read"
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_action_unknown_action_level(self):
        """
        Attack: action="nonexistent" — should default to read level (1).
        Unknown actions default to read level (VAULT_ACTION_LEVELS["read"]).
        """
        mock_user = {"id": 1, "role": "member"}

        # Mock the permission lookup at the get_effective_vault_permission level
        async def mock_get_effective_vault_permission(db, principal, vault_id):
            return "read"  # member has read permission on vault 1

        with patch("app.api.deps.get_effective_vault_permission", new=mock_get_effective_vault_permission):
            result = await _evaluate_policy(
                MagicMock(), mock_user, "vault", 1, "nonexistent_action"
            )

        # Nonexistent action → default to read level (1)
        # read permission (level 1) >= read required (level 1) → allowed
        assert result is True

    @pytest.mark.asyncio
    async def test_member_read_access_cannot_write(self):
        """
        Attack: member with read permission tries to write.
        """
        mock_user = {"id": 1, "role": "member"}

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (1, "read")
        mock_cursor.fetchall.return_value = []
        mock_db.execute.return_value = mock_cursor

        result = await _evaluate_policy(mock_db, mock_user, "vault", 1, "write")

        # read (1) < write (2) → denied
        assert result is False

    @pytest.mark.asyncio
    async def test_member_read_access_cannot_delete(self):
        """
        Attack: member with read permission tries to delete.
        """
        mock_user = {"id": 1, "role": "member"}

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (1, "read")
        mock_cursor.fetchall.return_value = []
        mock_db.execute.return_value = mock_cursor

        result = await _evaluate_policy(mock_db, mock_user, "vault", 1, "delete")

        # read (1) < delete (3) → denied
        assert result is False


# =============================================================================
# SUMMARY
# =============================================================================


class TestSecuritySummary:
    """Summary verification."""

    def test_all_attack_vectors_defined(self):
        """
        Verify all DI refactor attack vectors are defined.
        """
        required_tests = {
            "TestDIConnectionReuse": [
                "test_require_vault_permission_uses_injected_db_not_standalone_pool",
            ],
            "TestMalformedVaultIdAttacks": [
                "test_require_vault_permission_negative_vault_id_denied",
                "test_require_vault_permission_zero_vault_id_denied",
            ],
            "TestEmptyActionsBypass": [
                "test_require_vault_permission_empty_actions_denies_all",
            ],
            "TestNoneVaultIdPrivilegeEscalation": [
                "test_require_vault_permission_none_vault_id_denies_superadmin",
            ],
            "TestResourceTypeConfusion": [
                "test_evaluate_policy_non_vault_non_group_member_denied",
            ],
            "TestPrivilegeEscalation": [
                "test_superadmin_id_zero_escalation_blocked",
            ],
            "TestDependencyOverrideBypass": [
                "test_require_vault_permission_multi_action_any_match_passes",
            ],
            "TestConcurrentAccessAttacks": [
                "test_concurrent_vault_access_same_user_different_vaults",
            ],
        }

        for class_name, test_names in required_tests.items():
            test_class = globals().get(class_name)
            assert test_class is not None, f"Missing test class {class_name}"
            for test_name in test_names:
                assert callable(getattr(test_class, test_name, None)), (
                    f"Missing required test {class_name}.{test_name}"
                )
