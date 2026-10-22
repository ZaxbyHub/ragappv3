"""
Tests for visibility='org' permission fix in get_effective_vault_permissions.

Covers:
1. visibility='org' vault: user in same org → read granted
2. visibility='org' vault: user NOT in same org → no read granted
3. visibility='org' vault with org_id=NULL → no read granted (treated as private)
4. visibility='public' vault: any authenticated user → read granted (unchanged)
5. visibility='public' vault with org_id=N: user not in org N → read still granted (public always grants read)
6. visibility='private' vault: no automatic read access (unchanged)
"""

import sqlite3

import pytest

from app.api.deps import (
    get_effective_vault_permission,
    get_effective_vault_permissions,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def org_visibility_db():
    """In-memory DB with org-scoped visibility test data.

    Orgs: 10, 20
    Users: 5 (member of org 10), 6 (member of org 20), 99 (no org membership)

    Vault layout:
      1  visibility=public  org_id=NULL   → always readable
      2  visibility=public  org_id=10      → org-scoped public (org 10 members only)
      3  visibility=org     org_id=10      → org-scoped (org 10 members only)
      4  visibility=org     org_id=20      → org-scoped (org 20 members only)
      5  visibility=org     org_id=NULL    → treated as private (no automatic access)
      6  visibility=private  org_id=10     → no automatic access
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE vaults (id INTEGER PRIMARY KEY, visibility TEXT DEFAULT 'private', org_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE vault_members (vault_id INTEGER, user_id INTEGER, permission TEXT)"
    )
    conn.execute("CREATE TABLE group_members (group_id INTEGER, user_id INTEGER)")
    # groups table required by the reader-side group/org predicate (#721 AC4)
    conn.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE vault_group_access (vault_id INTEGER, group_id INTEGER, permission TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS org_members (org_id INTEGER NOT NULL, user_id INTEGER NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO vaults (id, visibility, org_id) VALUES (?, ?, ?)",
        [
            (1, "public",  None),   # vault 1: public, no org
            (2, "public",  10),      # vault 2: public, org-scoped to org 10
            (3, "org",     10),      # vault 3: org-visibility for org 10
            (4, "org",     20),      # vault 4: org-visibility for org 20
            (5, "org",     None),    # vault 5: org-visibility but no org → private
            (6, "private", 10),      # vault 6: private with org_id=10
        ],
    )
    conn.executemany(
        "INSERT INTO org_members (org_id, user_id) VALUES (?, ?)",
        [(10, 5), (20, 6)],  # user 5 → org 10, user 6 → org 20
    )
    conn.commit()
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────────────────────────────────────────


class TestVisibilityOrgPermission:
    """visibility='org' permission fix — FR-XXX/SC-XXX."""

    # ── 1. visibility='org' vault: user in same org → read granted ──────────

    async def test_org_visibility_user_in_same_org_gets_read(self, org_visibility_db):
        """Org member can read vaults with visibility='org' in their org."""
        user = {"id": 5, "role": "member"}  # member of org 10
        conn = org_visibility_db

        # Vault 3: visibility='org', org_id=10, user 5 is member of org 10
        assert await get_effective_vault_permission(conn, user, 3) == "read"

    async def test_org_visibility_user_in_same_org_batch(self, org_visibility_db):
        """Org member gets read on org-visibility vault via batch query."""
        user = {"id": 5, "role": "member"}
        conn = org_visibility_db

        result = await get_effective_vault_permissions(conn, user, [1, 2, 3, 4, 5, 6])
        assert result[3] == "read"

    # ── 2. visibility='org' vault: user NOT in same org → no read ──────────

    async def test_org_visibility_user_in_different_org_denied(self, org_visibility_db):
        """User who is NOT a member of the vault's org gets no read access."""
        user = {"id": 6, "role": "member"}  # member of org 20, NOT org 10
        conn = org_visibility_db

        # Vault 3: visibility='org', org_id=10, user 6 is in org 20 → denied
        assert await get_effective_vault_permission(conn, user, 3) is None

    async def test_org_visibility_user_in_different_org_cannot_see_it(self, org_visibility_db):
        """User in a different org does not see org-visibility vault in batch result."""
        user = {"id": 6, "role": "member"}
        conn = org_visibility_db

        result = await get_effective_vault_permissions(conn, user, [3, 4])
        assert result[3] is None
        # user 6 IS a member of org 20 so vault 4 should be accessible
        assert result[4] == "read"

    async def test_org_visibility_user_with_no_org_membership_denied(self, org_visibility_db):
        """User with no org membership at all gets no read on org-visibility vault."""
        user = {"id": 99, "role": "member"}  # no org membership
        conn = org_visibility_db

        assert await get_effective_vault_permission(conn, user, 3) is None
        assert await get_effective_vault_permission(conn, user, 4) is None

    # ── 3. visibility='org' vault with org_id=NULL → treated as private ────

    async def test_org_visibility_null_org_id_is_private(self, org_visibility_db):
        """Vault with visibility='org' but org_id=NULL grants no automatic read."""
        user = {"id": 5, "role": "member"}  # org 10 member
        conn = org_visibility_db

        # Vault 5: visibility='org', org_id=NULL → no automatic access
        assert await get_effective_vault_permission(conn, user, 5) is None

    async def test_org_visibility_null_org_id_batch_excluded(self, org_visibility_db):
        """Org-visibility vault with NULL org_id does not appear in batch results."""
        user = {"id": 5, "role": "member"}
        conn = org_visibility_db

        result = await get_effective_vault_permissions(conn, user, [1, 2, 3, 4, 5, 6])
        assert result[5] is None

    # ── 4. visibility='public' vault: any authenticated user → read ─────────

    async def test_public_visibility_no_org_id_always_readable(self, org_visibility_db):
        """Public vault with no org_id is readable by any authenticated user."""
        user = {"id": 99, "role": "member"}  # no org membership
        conn = org_visibility_db

        # Vault 1: visibility='public', org_id=NULL → always readable
        assert await get_effective_vault_permission(conn, user, 1) == "read"

    async def test_public_visibility_no_org_id_org_member_also_readable(self, org_visibility_db):
        """Public vault with no org_id is readable even by org members."""
        user = {"id": 5, "role": "member"}
        conn = org_visibility_db

        assert await get_effective_vault_permission(conn, user, 1) == "read"

    # ── 5. visibility='public' vault with org_id=N: user not in org N → read denied ─

    async def test_public_visibility_org_scoped_denies_non_org_members(self, org_visibility_db):
        """Public vault with org_id restricts read to org members only (FR-016/SC-009).

        This is the existing correct behavior for public+org_id vaults: org scoping
        is NOT just a listing filter — it also restricts read access to org members.
        Non-org-members get no automatic read even on public vaults.
        """
        user = {"id": 6, "role": "member"}  # member of org 20, NOT org 10
        conn = org_visibility_db

        # Vault 2: visibility='public', org_id=10 — user 6 is NOT in org 10 → denied
        assert await get_effective_vault_permission(conn, user, 2) is None

    async def test_public_visibility_org_scoped_user_in_same_org_gets_read(self, org_visibility_db):
        """Public vault with org_id is readable by users in the specified org."""
        user = {"id": 5, "role": "member"}  # member of org 10
        conn = org_visibility_db

        assert await get_effective_vault_permission(conn, user, 2) == "read"

    # ── 6. visibility='private' vault: no automatic read access ─────────────

    async def test_private_visibility_no_automatic_read(self, org_visibility_db):
        """Private vault grants no automatic read, even with org_id set."""
        user = {"id": 5, "role": "member"}  # org 10 member
        conn = org_visibility_db

        # Vault 6: visibility='private', org_id=10 — no automatic access
        assert await get_effective_vault_permission(conn, user, 6) is None

    async def test_private_visibility_batch_excluded(self, org_visibility_db):
        """Private vault does not appear in batch results for a regular user."""
        user = {"id": 5, "role": "member"}
        conn = org_visibility_db

        result = await get_effective_vault_permissions(conn, user, [1, 2, 3, 4, 5, 6])
        assert result[6] is None


# ─────────────────────────────────────────────────────────────────────────────
# Superadmin bypass — unchanged behavior
# ─────────────────────────────────────────────────────────────────────────────


class TestVisibilityOrgSuperadminBypass:
    """Superadmin always gets admin, org visibility rules are bypassed."""

    async def test_superadmin_bypasses_org_visibility(self, org_visibility_db):
        """Superadmin gets admin on all vaults including org-visibility vaults."""
        superadmin = {"id": 1, "role": "superadmin"}
        conn = org_visibility_db

        assert await get_effective_vault_permission(conn, superadmin, 3) == "admin"
        assert await get_effective_vault_permission(conn, superadmin, 4) == "admin"
        assert await get_effective_vault_permission(conn, superadmin, 5) == "admin"
