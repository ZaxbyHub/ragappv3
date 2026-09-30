"""Acceptance checks (issue #690, defect 1): orphan-user Default-vault grants.

Pins the desired post-fix behavior of
``migrate_assign_orphan_users_to_default_vault``:

- AC1: a direct vault membership that was deliberately removed must NOT be
  re-granted by a subsequent migration rerun.
- AC2: a user whose vault access already comes through a group grant must
  not receive a redundant direct ``vault_members`` row on the Default vault.
"""

import sqlite3

from app.models.database import (
    migrate_assign_orphan_users_to_default_vault,
    run_migrations,
)


def _insert_user(db_path: str, user_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role,"
            " is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (user_id, f"user{user_id}", "hash", f"User {user_id}", "member"),
        )
        conn.commit()
    finally:
        conn.close()


def _vault_member_count(db_path: str, user_id: int) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM vault_members WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_removed_member_not_regranted_on_rerun(tmp_path, monkeypatch):
    """AC1: deleting a user's only vault_members row must not be undone."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    db_path = str(tmp_path / "app.db")
    run_migrations(db_path)
    _insert_user(db_path, user_id=1)

    # First run grants the orphan user a direct 'read' row on the Default vault.
    migrate_assign_orphan_users_to_default_vault(db_path)
    assert _vault_member_count(db_path, 1) == 1

    # An admin revokes that direct membership.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM vault_members WHERE user_id = ?", (1,))
        conn.commit()
    finally:
        conn.close()

    # Rerunning the migration must not resurrect the removed membership.
    migrate_assign_orphan_users_to_default_vault(db_path)

    assert _vault_member_count(db_path, 1) == 0


def test_group_access_user_gets_no_direct_default_row(tmp_path, monkeypatch):
    """AC2: group-based access must not be duplicated as a direct row."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    db_path = str(tmp_path / "app.db")
    run_migrations(db_path)
    _insert_user(db_path, user_id=1)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute("SELECT id FROM vaults WHERE name = 'Default'").fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO vaults (name, description) VALUES (?, ?)",
                ("Default", "Default vault for all users"),
            )
            default_vault_id = cursor.lastrowid
        else:
            default_vault_id = row[0]

        cursor = conn.execute(
            "INSERT INTO organizations (name, description, slug, created_by)"
            " VALUES (?, ?, ?, ?)",
            ("Org One", "Desc", "org-one", 1),
        )
        org_id = cursor.lastrowid
        cursor = conn.execute(
            "INSERT INTO groups (org_id, name, description) VALUES (?, ?, ?)",
            (org_id, "Group One", "Desc"),
        )
        group_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, 1),
        )
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission)"
            " VALUES (?, ?, 'read')",
            (default_vault_id, group_id),
        )
        conn.commit()
    finally:
        conn.close()

    migrate_assign_orphan_users_to_default_vault(db_path)

    assert _vault_member_count(db_path, 1) == 0
