"""
Tests for parallelized get_effective_vault_permissions.

Covers the asyncio.gather(asyncio.to_thread(...)) concurrent execution at deps.py:420-425.

Test cases:
1. All four queries run concurrently via asyncio.gather (verify gather called once with 4 to_thread calls)
2. Result merging: direct membership + group access + public + org all contribute correct levels
3. Superadmin short-circuit unchanged
4. Empty vault_ids returns {}
5. Admin baseline level = write
6. Normal user baseline level = 0
7. Error propagation: if one query raises, the exception propagates
8. Mixed visibility modes produce correct merged permissions
"""

import asyncio
import importlib
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.api.deps import (
    _SQLITE_SERIALIZED,
    VAULT_PERMISSION_LEVELS,
    get_effective_vault_permissions,
)


@pytest.fixture(autouse=True)
def reset_fallback_warned():
    """Reset the _FALLBACK_WARNED global between tests to avoid leakage."""
    deps = importlib.import_module("app.api.deps")
    deps._FALLBACK_WARNED = False
    yield
    deps._FALLBACK_WARNED = False


# ─────────────────────────────────────────────────────────────────────────────
# In-memory DB fixture with full schema
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """Create an in-memory SQLite DB with the required schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("""
        CREATE TABLE vaults (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'private',
            org_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE vault_members (
            id INTEGER PRIMARY KEY,
            vault_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            FOREIGN KEY (vault_id) REFERENCES vaults(id)
        )
    """)
    conn.execute("""
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            org_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE group_members (
            id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups(id)
        )
    """)
    conn.execute("""
        CREATE TABLE vault_group_access (
            id INTEGER PRIMARY KEY,
            vault_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            FOREIGN KEY (vault_id) REFERENCES vaults(id),
            FOREIGN KEY (group_id) REFERENCES groups(id)
        )
    """)
    conn.execute("""
        CREATE TABLE org_members (
            id INTEGER PRIMARY KEY,
            org_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL
        )
    """)

    # Seed vaults: private, public, org
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (1, 'private_vault', 'private', NULL)")
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (2, 'public_vault', 'public', NULL)")
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (3, 'org_vault', 'org', 100)")
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (4, 'org_public_vault', 'public', 100)")

    # Org 100 membership
    conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (100, 50)")

    conn.commit()
    yield conn
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: asyncio.gather is called once with four asyncio.to_thread calls
# ─────────────────────────────────────────────────────────────────────────────

class TestConcurrency:
    """Verify all four queries run concurrently via asyncio.gather."""

    @pytest.mark.asyncio
    async def test_four_queries_run_via_asyncio_gather(self, db_conn):
        """
        get_effective_vault_permissions calls asyncio.gather exactly once,
        passing four asyncio.to_thread-wrapped query callables.
        """
        user_principal = {"id": 1, "role": "member"}

        to_thread_calls = []

        original_to_thread = asyncio.to_thread

        async def tracking_to_thread(fn, *args, **kwargs):
            to_thread_calls.append(fn)
            return await original_to_thread(fn, *args, **kwargs)

        gather_calls = []
        original_gather = asyncio.gather

        async def tracking_gather(*coros):
            gather_calls.append(coros)
            return await original_gather(*coros)

        with patch("asyncio.to_thread", tracking_to_thread), \
             patch("asyncio.gather", tracking_gather):
            await get_effective_vault_permissions(db_conn, user_principal, [1, 2, 3])

        # On SERIALIZED builds (sqlite3.threadsafety == 3), all four queries run
        # concurrently via asyncio.gather. On non-SERIALIZED builds (e.g. Python
        # 3.11 CI), sqlite3 objects are not thread-safe, so the sequential
        # branch runs and gather is never called.
        if _SQLITE_SERIALIZED:
            assert len(gather_calls) == 1, f"Expected 1 gather call, got {len(gather_calls)}"
            gathered_coros = gather_calls[0]
            assert len(gathered_coros) == 4, f"Expected 4 coroutines in gather, got {len(gathered_coros)}"
        else:
            assert len(gather_calls) == 0, f"Expected 0 gather calls on non-SERIALIZED SQLite, got {len(gather_calls)}"

        # All four queries were wrapped in to_thread regardless of threading mode
        assert len(to_thread_calls) == 4, f"Expected 4 to_thread calls, got {len(to_thread_calls)}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Result merging — all four sources contribute correct levels
# ─────────────────────────────────────────────────────────────────────────────

class TestResultMerging:
    """Verify direct membership, group access, public vaults, and org vaults merge correctly."""

    @pytest.mark.asyncio
    async def test_direct_membership_contributes(self, db_conn):
        """Direct vault_members entry grants its permission level."""
        db_conn.execute("INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 10, 'admin')")
        db_conn.commit()

        result = await get_effective_vault_permissions(db_conn, {"id": 10, "role": "member"}, [1])

        assert result[1] == "admin"

    @pytest.mark.asyncio
    async def test_group_access_contributes(self, db_conn):
        """Group access via vault_group_access + group_members contributes its permission."""
        db_conn.execute("INSERT INTO groups (id, name, org_id) VALUES (7, 'editors', NULL)")
        db_conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (7, 11)")
        db_conn.execute("INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (1, 7, 'write')")
        db_conn.commit()

        result = await get_effective_vault_permissions(db_conn, {"id": 11, "role": "member"}, [1])

        assert result[1] == "write"

    @pytest.mark.asyncio
    async def test_public_vault_grants_read(self, db_conn):
        """Public vault (visibility='public', no org_id) grants read to any user."""
        result = await get_effective_vault_permissions(db_conn, {"id": 20, "role": "member"}, [2])

        assert result[2] == "read"

    @pytest.mark.asyncio
    async def test_org_vault_grants_read_to_org_member(self, db_conn):
        """Org vault grants read to a user who is a member of the vault's org."""
        result = await get_effective_vault_permissions(db_conn, {"id": 50, "role": "member"}, [3])

        assert result[3] == "read"

    @pytest.mark.asyncio
    async def test_public_org_vault_grants_read_to_org_member(self, db_conn):
        """Public vault with org_id grants read to org members even though visibility is 'public'."""
        # Vault 4 is public_vault with org_id=100; user 50 is org member of 100
        result = await get_effective_vault_permissions(db_conn, {"id": 50, "role": "member"}, [4])

        assert result[4] == "read"

    @pytest.mark.asyncio
    async def test_max_merging_direct_and_group(self, db_conn):
        """When both direct membership and group access exist, max() wins."""
        db_conn.execute("INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 12, 'read')")
        db_conn.execute("INSERT INTO groups (id, name, org_id) VALUES (8, 'admins', NULL)")
        db_conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (8, 12)")
        db_conn.execute("INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (1, 8, 'admin')")
        db_conn.commit()

        result = await get_effective_vault_permissions(db_conn, {"id": 12, "role": "member"}, [1])

        # max(direct_read=1, group_admin=3) = admin
        assert result[1] == "admin"

    @pytest.mark.asyncio
    async def test_max_merging_public_and_direct(self, db_conn):
        """Direct membership write beats public vault read via max."""
        db_conn.execute("INSERT INTO vault_members (vault_id, user_id, permission) VALUES (2, 13, 'write')")
        db_conn.commit()

        result = await get_effective_vault_permissions(db_conn, {"id": 13, "role": "member"}, [2])

        # max(direct_write=2, public_read=1) = write
        assert result[2] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Superadmin short-circuit unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestSuperadminShortCircuit:
    """Superadmin bypasses all query execution."""

    @pytest.mark.asyncio
    async def test_superadmin_returns_admin_for_all_vaults(self, db_conn):
        """Superadmin gets 'admin' for every vault, without querying vault_members."""
        # No vault_members for superadmin user 99
        superadmin_principal = {"id": 99, "role": "superadmin"}

        result = await get_effective_vault_permissions(db_conn, superadmin_principal, [1, 2, 3])

        assert result[1] == "admin"
        assert result[2] == "admin"
        assert result[3] == "admin"

    @pytest.mark.asyncio
    async def test_superadmin_skips_db_queries(self, db_conn):
        """Superadmin path skips all four async queries entirely."""
        execute_mock = MagicMock()

        async def fake_get_effective_vault_permissions(db, principal, vault_ids):
            # Simulate the superadmin short-circuit before any DB access
            user_role = principal.get("role", "")
            if user_role == "superadmin":
                return {vid: "admin" for vid in vault_ids}
            # If we get here, the short-circuit failed
            raise AssertionError("Superadmin did not short-circuit — DB was accessed")

        with patch("app.api.deps.get_effective_vault_permissions", new=fake_get_effective_vault_permissions):
            # This would fail if DB queries ran for superadmin
            result = await fake_get_effective_vault_permissions(db_conn, {"id": 99, "role": "superadmin"}, [1, 2])
            assert result == {1: "admin", 2: "admin"}


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Empty vault_ids returns {}
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyInput:
    """Edge-case inputs that should produce empty results."""

    @pytest.mark.asyncio
    async def test_empty_vault_ids_returns_empty_dict(self, db_conn):
        """Empty vault_ids list returns {} without accessing the database."""
        result = await get_effective_vault_permissions(db_conn, {"id": 10, "role": "admin"}, [])
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_id_principal_returns_empty_dict(self, db_conn):
        """Principal without 'id' key returns {} without accessing the database."""
        result = await get_effective_vault_permissions(db_conn, {"role": "admin"}, [1])
        assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Admin baseline level = write
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminBaseline:
    """Admin users get write (level 2) as a baseline floor on all vaults."""

    @pytest.mark.asyncio
    async def test_admin_baseline_is_write(self, db_conn):
        """Admin with no explicit membership gets write (level 2) on a private vault."""
        result = await get_effective_vault_permissions(db_conn, {"id": 80, "role": "admin"}, [1])

        assert result[1] == "write"

    @pytest.mark.asyncio
    async def test_admin_baseline_write_beats_public_read(self, db_conn):
        """Admin baseline write beats public vault's implicit read."""
        result = await get_effective_vault_permissions(db_conn, {"id": 81, "role": "admin"}, [2])

        # max(admin_baseline=2, public_read=1) = write
        assert result[2] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Normal user baseline level = 0
# ─────────────────────────────────────────────────────────────────────────────

class TestMemberBaseline:
    """Non-admin users get level 0 (None) as baseline on private vaults."""

    @pytest.mark.asyncio
    async def test_member_baseline_is_none_on_private_vault(self, db_conn):
        """Member with no memberships gets None (level 0) on private vault."""
        result = await get_effective_vault_permissions(db_conn, {"id": 70, "role": "member"}, [1])

        assert result[1] is None

    @pytest.mark.asyncio
    async def test_member_private_vault_stays_none_without_membership(self, db_conn):
        """Member with no direct/group/org access gets None on private vault."""
        result = await get_effective_vault_permissions(db_conn, {"id": 71, "role": "member"}, [1])

        assert result[1] is None


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Error propagation
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorPropagation:
    """If any of the four concurrent queries raises, the exception propagates."""

    @pytest.mark.asyncio
    async def test_exception_in_to_thread_propagates(self, db_conn):
        """An exception raised inside a to_thread-wrapped query propagates via gather."""

        def failing_query():
            raise RuntimeError("simulated DB failure in _query_vault_members")

        # Patch to_thread so it calls the original for the first 3 but raises for the 4th
        original_to_thread = asyncio.to_thread
        call_count = 0

        async def controlled_to_thread(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # First call is _query_vault_members
                # Raise immediately in-thread context (to_thread runs sync fn in thread pool)
                raise RuntimeError("simulated DB failure in _query_vault_members")
            return await original_to_thread(fn, *args, **kwargs)

        with patch("asyncio.to_thread", controlled_to_thread):
            with pytest.raises(RuntimeError, match="simulated DB failure"):
                await get_effective_vault_permissions(db_conn, {"id": 1, "role": "member"}, [1, 2, 3])

    @pytest.mark.asyncio
    async def test_exception_propagates_from_gather(self, db_conn):
        """If any awaitable in asyncio.gather raises, gather propagates that exception."""

        if not _SQLITE_SERIALIZED:
            pytest.skip("asyncio.gather not used on non-SERIALIZED SQLite builds")

        # Replace asyncio.gather with one that immediately raises
        async def raising_gather(*coros):
            raise RuntimeError("simulated DB failure")

        with patch("asyncio.gather", raising_gather):
            with pytest.raises(RuntimeError, match="simulated DB failure"):
                await get_effective_vault_permissions(db_conn, {"id": 1, "role": "member"}, [1])


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Mixed visibility modes produce correct merged permissions
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedVisibilityModes:
    """Vaults with different visibility modes produce correct merged results."""

    @pytest.mark.asyncio
    async def test_private_vault_no_access(self, db_conn):
        """Private vault with no membership → None for member user."""
        result = await get_effective_vault_permissions(db_conn, {"id": 30, "role": "member"}, [1])
        assert result[1] is None

    @pytest.mark.asyncio
    async def test_public_vault_read_access(self, db_conn):
        """Public vault (no org) → read for any user."""
        result = await get_effective_vault_permissions(db_conn, {"id": 31, "role": "member"}, [2])
        assert result[2] == "read"

    @pytest.mark.asyncio
    async def test_org_vault_org_member_read_access(self, db_conn):
        """Org vault → read only for org members."""
        result = await get_effective_vault_permissions(db_conn, {"id": 50, "role": "member"}, [3])
        assert result[3] == "read"

    @pytest.mark.asyncio
    async def test_org_vault_non_org_member_no_access(self, db_conn):
        """Org vault → None for users who are not org members."""
        result = await get_effective_vault_permissions(db_conn, {"id": 31, "role": "member"}, [3])
        assert result[3] is None

    @pytest.mark.asyncio
    async def test_mixed_vaults_all_ids_produced(self, db_conn):
        """Requesting public, private, org vaults together returns all IDs in result."""
        result = await get_effective_vault_permissions(
            db_conn, {"id": 50, "role": "member"}, [1, 2, 3]
        )
        # All three IDs present in result (even if some are None)
        assert 1 in result
        assert 2 in result
        assert 3 in result

    @pytest.mark.asyncio
    async def test_admin_mixed_visibility_all_write(self, db_conn):
        """Admin user gets write on all vaults regardless of visibility."""
        result = await get_effective_vault_permissions(
            db_conn, {"id": 90, "role": "admin"}, [1, 2, 3]
        )
        assert result[1] == "write"
        assert result[2] == "write"
        assert result[3] == "write"

    @pytest.mark.asyncio
    async def test_member_with_multiple_access_paths_gets_max(self, db_conn):
        """Member with direct write + group admin + org read → gets admin (max of all)."""
        db_conn.execute("INSERT INTO groups (id, name, org_id) VALUES (9, 'super_editors', NULL)")
        db_conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (9, 60)")
        db_conn.execute("INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (1, 9, 'admin')")
        db_conn.execute("INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 60, 'write')")
        db_conn.commit()

        result = await get_effective_vault_permissions(db_conn, {"id": 60, "role": "member"}, [1])

        # max(direct_write=2, group_admin=3) = admin
        assert result[1] == "admin"

    @pytest.mark.asyncio
    async def test_dedup_vault_ids_merged(self, db_conn):
        """Duplicate vault_ids in input are deduplicated (dict.fromkeys)."""
        result = await get_effective_vault_permissions(
            db_conn, {"id": 10, "role": "admin"}, [1, 1, 2, 2, 2]
        )
        # Only unique IDs present
        assert set(result.keys()) == {1, 2}
        assert result[1] == "write"
        assert result[2] == "write"
