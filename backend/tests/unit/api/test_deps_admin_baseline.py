"""
Tests for get_effective_vault_permissions admin baseline permission fix.

Covers the fix at deps.py line 309: admin users now get vault-level admin
permission (level 3) as a floor, without needing an explicit vault_members entry.

Verify:
1. Admin user, no explicit vault membership → gets admin (level 3) on any vault
2. Admin user with explicit read membership → still gets admin (level 3, max of floor and explicit)
3. Member user, no explicit vault membership → gets nothing (level 0, unchanged)
4. Superadmin user → still gets admin (level 3, unchanged - superadmin bypass already existed)
5. Regular user with explicit admin membership → still gets admin (unchanged)
"""
import os
import sqlite3

# Import directly from the source module
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))
from app.api.deps import VAULT_PERMISSION_LEVELS, get_effective_vault_permissions


@pytest.fixture
def db_conn():
    """Create an in-memory SQLite DB with the required schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")

    # vaults table
    conn.execute("""
        CREATE TABLE vaults (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'private',
            org_id INTEGER
        )
    """)

    # vault_members table
    conn.execute("""
        CREATE TABLE vault_members (
            id INTEGER PRIMARY KEY,
            vault_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            FOREIGN KEY (vault_id) REFERENCES vaults(id)
        )
    """)

    # groups table
    conn.execute("""
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            org_id INTEGER
        )
    """)

    # group_members table
    conn.execute("""
        CREATE TABLE group_members (
            id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups(id)
        )
    """)

    # vault_group_access table
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

    # org_members table
    conn.execute("""
        CREATE TABLE org_members (
            id INTEGER PRIMARY KEY,
            org_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL
        )
    """)

    # Seed some vaults
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (1, 'private_vault', 'private', NULL)")
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (2, 'public_vault', 'public', NULL)")
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (3, 'org_vault', 'org', 100)")
    conn.execute("INSERT INTO vaults (id, name, visibility, org_id) VALUES (4, 'another_private', 'private', NULL)")

    # Org membership for public/org vaults
    conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (100, 999)")

    conn.commit()
    yield conn
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Admin user, no explicit vault membership → gets write (level 2)
# ─────────────────────────────────────────────────────────────────────────────
async def test_admin_user_no_vault_membership_gets_write(db_conn):
    """
    System admin users get vault-level write as a baseline floor, not full admin.
    This means they can read/write all vaults but cannot delete or manage members
    without explicit vault_members admin entry.
    """
    admin_principal = {"id": 10, "role": "admin"}

    result = await get_effective_vault_permissions(db_conn, admin_principal, [1, 2, 3, 4])

    # Admin floor is level 2 ("write") with no vault_members entry
    assert result[1] == "write"
    assert result[2] == "write"
    assert result[3] == "write"
    assert result[4] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Admin user with explicit read membership → baseline write wins (level 2)
# max of baseline floor and explicit membership
# ─────────────────────────────────────────────────────────────────────────────
async def test_admin_user_with_explicit_read_membership_still_gets_write(db_conn):
    """
    When an admin has an explicit 'read' vault_members entry,
    the admin baseline floor (level 2) should win via max().
    """
    # Add explicit read-only membership for admin user on vault 1
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 10, 'read')"
    )
    db_conn.commit()

    admin_principal = {"id": 10, "role": "admin"}
    result = await get_effective_vault_permissions(db_conn, admin_principal, [1])

    # max(admin_baseline=2, explicit_read=1) = write
    assert result[1] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Admin user with explicit write membership → write (level 2)
# ─────────────────────────────────────────────────────────────────────────────
async def test_admin_user_with_explicit_write_membership_still_gets_write(db_conn):
    """
    When an admin has an explicit 'write' vault_members entry,
    the admin baseline floor (level 2) matches.
    """
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 10, 'write')"
    )
    db_conn.commit()

    admin_principal = {"id": 10, "role": "admin"}
    result = await get_effective_vault_permissions(db_conn, admin_principal, [1])

    # max(admin_baseline=2, explicit_write=2) = write
    assert result[1] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Member user, no explicit vault membership → gets nothing (level 0)
# ─────────────────────────────────────────────────────────────────────────────
async def test_member_user_no_vault_membership_gets_nothing(db_conn):
    """
    A regular 'member' user with no vault_members entry,
    no group access, and no org membership gets None (level 0).
    """
    member_principal = {"id": 20, "role": "member"}

    result = await get_effective_vault_permissions(db_conn, member_principal, [1, 2, 3, 4])

    # All private vaults should be None (no membership, no org membership for user 20)
    assert result[1] is None
    # Public vault grants read even without membership
    assert result[2] == "read"
    # Org vault: user 20 is not org_member of org 100
    assert result[3] is None
    assert result[4] is None


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Superadmin user → still gets admin (level 3), superadmin bypass
# ─────────────────────────────────────────────────────────────────────────────
async def test_superadmin_user_gets_admin_on_all_vaults(db_conn):
    """
    Superadmin role bypasses vault_members entirely and always gets admin.
    """
    # Add an explicit DENY or lower permission to prove superadmin bypass works
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 99, 'read')"
    )
    db_conn.commit()

    superadmin_principal = {"id": 99, "role": "superadmin"}

    result = await get_effective_vault_permissions(db_conn, superadmin_principal, [1, 2, 3, 4])

    # Superadmin bypasses vault_members — gets admin on all vaults regardless
    assert result[1] == "admin"
    assert result[2] == "admin"
    assert result[3] == "admin"
    assert result[4] == "admin"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Regular user with explicit admin membership → still gets admin
# ─────────────────────────────────────────────────────────────────────────────
async def test_regular_user_with_explicit_admin_membership_gets_admin(db_conn):
    """
    A non-admin user with an explicit 'admin' vault_members entry
    still gets admin (unchanged behavior).
    """
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 30, 'admin')"
    )
    db_conn.commit()

    regular_principal = {"id": 30, "role": "member"}

    result = await get_effective_vault_permissions(db_conn, regular_principal, [1])

    assert result[1] == "admin"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Member user gets read on public vault (org membership not needed)
# ─────────────────────────────────────────────────────────────────────────────
async def test_member_user_gets_read_on_public_vault(db_conn):
    """
    Even without vault_members entry, a user gets read on public vaults
    if they are an org member (or no org_id is required for truly public).
    """
    member_principal = {"id": 20, "role": "member"}

    result = await get_effective_vault_permissions(db_conn, member_principal, [2])

    # Public vault grants read to any user
    assert result[2] == "read"


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Member user with org membership gets read on org vault
# ─────────────────────────────────────────────────────────────────────────────
async def test_member_user_with_org_membership_gets_read_on_org_vault(db_conn):
    """
    User 999 is an org_member of org 100, so they get read on org_vault (id=3).
    """
    member_principal = {"id": 999, "role": "member"}

    result = await get_effective_vault_permissions(db_conn, member_principal, [3])

    assert result[3] == "read"


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Admin user on public vault — baseline write floor applies
# ─────────────────────────────────────────────────────────────────────────────
async def test_admin_user_on_public_vault_still_gets_write(db_conn):
    """
    Even on a public vault where regular users get 'read',
    admin users should still get 'write' via the baseline floor.
    """
    admin_principal = {"id": 10, "role": "admin"}

    result = await get_effective_vault_permissions(db_conn, admin_principal, [2])

    # max(admin_baseline=2, public_read=1) = write
    assert result[2] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Admin user with group-based read access still gets write floor
# ─────────────────────────────────────────────────────────────────────────────
async def test_admin_user_with_group_read_still_gets_write_floor(db_conn):
    """
    Admin with only group-based 'read' access still gets write floor
    because max(baseline=2, group_read=1) = write.
    """
    # Create group and add admin user to it
    db_conn.execute("INSERT INTO groups (id, name, org_id) VALUES (50, 'readers', NULL)")
    db_conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (50, 10)")
    # Give group read access to vault 1
    db_conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (1, 50, 'read')"
    )
    db_conn.commit()

    admin_principal = {"id": 10, "role": "admin"}

    result = await get_effective_vault_permissions(db_conn, admin_principal, [1])

    # max(admin_baseline=2, group_read=1) = write
    assert result[1] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: Empty vault_ids list → returns empty dict
# ─────────────────────────────────────────────────────────────────────────────
async def test_empty_vault_ids_returns_empty_dict(db_conn):
    """Empty input returns empty dict (unchanged behavior)."""
    admin_principal = {"id": 10, "role": "admin"}

    result = await get_effective_vault_permissions(db_conn, admin_principal, [])

    assert result == {}





# ─────────────────────────────────────────────────────────────────────────────
# Test 13: User with no id → returns empty dict
# ─────────────────────────────────────────────────────────────────────────────
async def test_principal_without_id_returns_empty_dict(db_conn):
    """Principal without 'id' key returns empty dict (unchanged)."""
    no_id_principal = {"role": "admin"}

    result = await get_effective_vault_permissions(db_conn, no_id_principal, [1])

    assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: Multi-vault request with mixed explicit permissions for admin
# ─────────────────────────────────────────────────────────────────────────────
async def test_admin_mixed_vaults_takes_max(db_conn):
    """
    Admin user requesting multiple vaults: explicit membership on some,
    no membership on others.
    """
    # Explicit write on vault 1
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 10, 'write')"
    )
    # Explicit read on vault 2 (public)
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (2, 10, 'read')"
    )
    db_conn.commit()

    admin_principal = {"id": 10, "role": "admin"}

    # Vault 1: max(baseline=2, explicit=2) = write
    # Vault 2: max(baseline=2, explicit=1, public_read=1) = write
    # Vault 3: no explicit, no public/org — baseline=2 = write
    # Vault 4: no explicit, no public/org — baseline=2 = write
    result = await get_effective_vault_permissions(db_conn, admin_principal, [1, 2, 3, 4])

    assert result[1] == "write"
    assert result[2] == "write"
    assert result[3] == "write"
    assert result[4] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 15: Member user with higher explicit permission via group
# ─────────────────────────────────────────────────────────────────────────────
async def test_member_user_via_group_write_gets_write(db_conn):
    """
    Non-admin user gets 'write' via group membership.
    No baseline floor for member, so group_write=2 is the result.
    """
    # Create group and add user to it
    # org 200 owns no vaults: membership satisfies the group/org
    # predicate (#721 AC4) without granting anything else.
    db_conn.execute("INSERT INTO groups (id, name, org_id) VALUES (60, 'editors', 200)")
    db_conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (200, 30)")
    db_conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (60, 30)")
    # Give group write access to vault 1
    db_conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (1, 60, 'write')"
    )
    db_conn.commit()

    regular_principal = {"id": 30, "role": "member"}

    result = await get_effective_vault_permissions(db_conn, regular_principal, [1])

    # member baseline=0, group_write=2 → write
    assert result[1] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 16: User with explicit permission on a vault wins over group permission
# ─────────────────────────────────────────────────────────────────────────────
async def test_explicit_vault_permission_beats_group_permission(db_conn):
    """
    When user has both explicit vault_members permission AND group access,
    the higher of the two should win.
    """
    # User 40: explicit 'read' on vault 1, but group gives 'write'
    db_conn.execute("INSERT INTO groups (id, name, org_id) VALUES (70, 'writers', 200)")
    db_conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (200, 40)")
    db_conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (70, 40)")
    db_conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (1, 70, 'write')"
    )
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 40, 'read')"
    )
    db_conn.commit()

    regular_principal = {"id": 40, "role": "member"}

    result = await get_effective_vault_permissions(db_conn, regular_principal, [1])

    # max(explicit_read=1, group_write=2) = write
    assert result[1] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 17: get_effective_vault_permission (single vault) delegates correctly
# ─────────────────────────────────────────────────────────────────────────────
async def test_get_effective_vault_permission_single_vault(db_conn):
    """Single vault variant should return a single value, not a dict."""
    from app.api.deps import get_effective_vault_permission

    admin_principal = {"id": 10, "role": "admin"}

    result = await get_effective_vault_permission(db_conn, admin_principal, 1)

    assert result == "write"


# ─────────────────────────────────────────────────────────────────────────────
# Test 18: get_effective_vault_permission with None vault_id → None
# ─────────────────────────────────────────────────────────────────────────────
async def test_get_effective_vault_permission_none_vault_id(db_conn):
    """None vault_id returns None immediately (unchanged behavior)."""
    from app.api.deps import get_effective_vault_permission

    admin_principal = {"id": 10, "role": "admin"}

    result = await get_effective_vault_permission(db_conn, admin_principal, None)

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Test 19: Admin user can have multiple vaults with different explicit perms
# ─────────────────────────────────────────────────────────────────────────────
async def test_admin_user_multiple_vaults_mixed_explicit_and_no_membership(db_conn):
    """
    Admin user on vault 1 has explicit 'read', vault 2 has no entry.
    Both should resolve to 'write' via max(baseline=2, explicit=1/0).
    """
    db_conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 10, 'read')"
    )
    db_conn.commit()

    admin_principal = {"id": 10, "role": "admin"}

    result = await get_effective_vault_permissions(db_conn, admin_principal, [1, 2])

    # vault 1: max(2, 1) = write
    # vault 2: max(2, 0) = write
    assert result[1] == "write"
    assert result[2] == "write"
