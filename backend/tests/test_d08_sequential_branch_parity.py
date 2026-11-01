"""Sequential-vs-concurrent branch parity for get_effective_vault_permissions.

The branch is selected by THIS module's global ``_SQLITE_SERIALIZED``
(app/services/authz_policy.py), not by the copy re-imported into
app.api.deps. This file forces each branch explicitly via monkeypatch and
asserts both merge the same fixture identically, and that the sequential
run actually executed (the once-per-process fallback warning fires when
``_FALLBACK_WARNED`` is reset).

Expected to PASS at base: both branches already exist and must agree.
"""

import asyncio
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.models.database import init_db
from app.services import authz_policy
from app.services.authz_policy import get_effective_vault_permissions


@pytest.fixture
def parity_db():
    """Temp file DB via init_db seeded with all four grant classes for one
    role=admin principal: direct membership (admin), a group-derived grant
    (read), the role baseline (write), and a public vault (read)."""
    temp_dir = tempfile.mkdtemp(prefix="d08_parity_")
    db_path = str(Path(temp_dir) / "parity.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, role, is_active)"
        " VALUES (1, 'parityadmin', 'x', 'admin', 1)"
    )
    org_id = conn.execute(
        "INSERT INTO organizations (name) VALUES ('Parity Org')"
    ).lastrowid
    # The group-derived grant needs the org-membership predicate (#721 AC4).
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, 1, 'member')",
        (org_id,),
    )
    group_id = conn.execute(
        "INSERT INTO groups (org_id, name) VALUES (?, 'Parity Group')", (org_id,)
    ).lastrowid
    conn.execute(
        "INSERT INTO group_members (group_id, user_id) VALUES (?, 1)", (group_id,)
    )

    direct_vault = conn.execute(
        "INSERT INTO vaults (name, visibility) VALUES ('Parity Direct', 'private')"
    ).lastrowid
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 1, 'admin', 1)",
        (direct_vault,),
    )

    group_vault = conn.execute(
        "INSERT INTO vaults (name, visibility) VALUES ('Parity Group', 'private')"
    ).lastrowid
    conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'read', 1)",
        (group_vault, group_id),
    )

    public_vault = conn.execute(
        "INSERT INTO vaults (name, visibility) VALUES ('Parity Public', 'public')"
    ).lastrowid

    baseline_vault = conn.execute(
        "INSERT INTO vaults (name, visibility) VALUES ('Parity Baseline', 'private')"
    ).lastrowid
    conn.commit()

    vault_ids = [direct_vault, group_vault, public_vault, baseline_vault]
    yield conn, {"id": 1, "role": "admin"}, vault_ids

    conn.close()
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_sequential_and_concurrent_branches_merge_identically(
    parity_db, caplog, monkeypatch
):
    conn, principal, vault_ids = parity_db

    # Sequential leg: force the module global off, reset the once-per-process
    # warning latch, and prove the sequential branch really ran.
    monkeypatch.setattr(authz_policy, "_SQLITE_SERIALIZED", False)
    monkeypatch.setattr(authz_policy, "_FALLBACK_WARNED", False)
    with caplog.at_level(logging.WARNING):
        result_seq = await get_effective_vault_permissions(
            conn, principal, list(vault_ids)
        )
    warning_fired = any(
        record.name.startswith("app.services.authz_policy")
        and record.levelno >= logging.WARNING
        and "sequentially" in record.getMessage()
        for record in caplog.records
    )
    assert warning_fired is True

    # Concurrent leg: force the gather branch on, same fixture, fresh read.
    monkeypatch.setattr(authz_policy, "_SQLITE_SERIALIZED", True)
    result_par = await get_effective_vault_permissions(
        conn, principal, list(vault_ids)
    )

    assert result_seq == result_par

    direct_vault, group_vault, public_vault, baseline_vault = vault_ids
    expected = {
        direct_vault: "admin",  # direct membership beats the write baseline
        group_vault: "write",  # group read capped by the admin-role baseline
        public_vault: "write",  # public read capped by the admin-role baseline
        baseline_vault: "write",  # role baseline alone
    }
    assert result_seq == expected
    assert result_par == expected
