"""End-to-end org -> group -> vault -> user access chain (issue #202, AC5).

Pins the full chain on the REAL schema: org membership + group membership +
group-vault assignment (vault_group_access) grant effective vault permission to
a user with NO direct vault_members row. The vault is private, so the
vault_group_access UNION branch in get_effective_vault_permissions is the only
possible grant source for the positive case.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"

import pytest  # noqa: E402

from app.models.database import init_db, run_migrations  # noqa: E402
from app.services.authz_policy import get_effective_vault_permission  # noqa: E402


@pytest.fixture
def chain_db():
    """Real-schema database seeded with the complete access chain."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "chain.db")
    init_db(db_path)
    run_migrations(db_path)

    conn = asyncio.run(_seed(db_path))
    yield conn
    conn.close()

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


async def _seed(db_path):
    import sqlite3

    # check_same_thread=False: get_effective_vault_permissions runs its queries
    # via asyncio.to_thread, so the connection is used from another thread.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO organizations (id, name) VALUES (100, 'Chain Org')")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, role, is_active) "
        "VALUES (?, ?, ?, ?, ?)",
        (10, "chainuser", "x", "member", 1),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, role, is_active) "
        "VALUES (?, ?, ?, ?, ?)",
        (11, "orgonlyuser", "x", "member", 1),
    )
    conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (100, 10)")
    conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (100, 11)")
    conn.execute(
        "INSERT INTO groups (id, org_id, name) VALUES (200, 100, 'Chain Editors')"
    )
    conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (200, 10)")
    conn.execute(
        "INSERT INTO vaults (id, name, description, owner_id, visibility) "
        "VALUES (?, ?, ?, ?, ?)",
        (300, "Chain Vault", "", 10, "private"),
    )
    conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, permission) "
        "VALUES (?, ?, ?)",
        (300, 200, "write"),
    )
    conn.commit()
    return conn


@pytest.mark.asyncio
async def test_org_group_vault_chain_grants_effective_permission(chain_db):
    """Group-derived permission reaches a user with zero direct membership."""
    user = {"id": 10, "role": "member"}

    # Precondition: the grant cannot come from vault_members.
    direct = chain_db.execute(
        "SELECT COUNT(*) FROM vault_members WHERE user_id = ? AND vault_id = ?",
        (10, 300),
    ).fetchone()[0]
    assert direct == 0

    permission = await get_effective_vault_permission(chain_db, user, 300)
    assert permission == "write", (
        "org membership + group membership + vault_group_access must grant "
        "exactly the group's permission (vault_group_access UNION branch)"
    )


@pytest.mark.asyncio
async def test_org_membership_alone_does_not_grant_group_scoped_vault(chain_db):
    """A same-org user outside the group gets nothing on the private vault."""
    org_only = {"id": 11, "role": "member"}
    permission = await get_effective_vault_permission(chain_db, org_only, 300)
    assert permission is None
