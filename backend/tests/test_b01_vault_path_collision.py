"""Acceptance checks (issue #690, defect 2): vault name/id directory collision.

Pins the desired post-fix behavior of ``migrate_vault_paths``: a vault whose
NAME sanitizes to the canonical id-directory of a DIFFERENT vault must not
cause that other vault's storage directory to be renamed or merged away.
"""

import sqlite3
from unittest.mock import patch

from app.models.database import migrate_vault_paths


def _make_db_and_vaults_dir(tmp_path, rows):
    """Create a minimal vaults table plus an empty vaults directory."""
    vaults_dir = tmp_path / "vaults"
    vaults_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE vaults (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.executemany("INSERT INTO vaults (id, name) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path, vaults_dir


def test_name_equal_to_other_vault_id_does_not_rename_its_dir(tmp_path):
    """AC3: vault 1 named '2' must not rename vault 2's canonical directory."""
    db_path, vaults_dir = _make_db_and_vaults_dir(tmp_path, [(1, "2"), (2, "other")])

    # Vault 2's canonical id-directory with its stored file.
    dir_two = vaults_dir / "2"
    dir_two.mkdir(parents=True, exist_ok=True)
    (dir_two / "a.txt").write_text("vault2")

    with patch("app.models.database.settings") as mock_settings:
        mock_settings.vaults_dir = vaults_dir
        migrate_vault_paths(str(db_path))

    present = int((vaults_dir / "2").is_dir())
    assert present == 1


def test_name_equal_to_other_vault_id_does_not_merge_its_dir(tmp_path):
    """AC4: vault 1 named '2' must not merge vault 2's directory into its own."""
    db_path, vaults_dir = _make_db_and_vaults_dir(tmp_path, [(1, "2"), (2, "other")])

    # Vault 1's canonical directory already exists (post-migration layout)...
    dir_one = vaults_dir / "1"
    dir_one.mkdir(parents=True, exist_ok=True)
    (dir_one / "x.txt").write_text("vault1")
    # ...and vault 2's canonical directory holds its data.
    dir_two = vaults_dir / "2"
    dir_two.mkdir(parents=True, exist_ok=True)
    (dir_two / "a.txt").write_text("vault2")

    with patch("app.models.database.settings") as mock_settings:
        mock_settings.vaults_dir = vaults_dir
        migrate_vault_paths(str(db_path))

    present = int((vaults_dir / "2" / "a.txt").is_file())
    assert present == 1
