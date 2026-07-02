"""Tests for vault path migration - renaming vaults/{sanitized_name}/ to vaults/{id}/."""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from app.models.database import migrate_vault_paths


class TestVaultPathMigration:
    """Tests for the migrate_vault_paths function."""

    @pytest.fixture
    def temp_db_and_vaults(self, tmp_path):
        """Create a temporary database with vaults table and vaults directory."""
        # Create vaults directory
        vaults_dir = tmp_path / "vaults"
        vaults_dir.mkdir(parents=True, exist_ok=True)

        # Create SQLite database with vaults table
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE vaults (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        return {"db_path": db_path, "vaults_dir": vaults_dir, "tmp_path": tmp_path}

    def test_branch_1_old_exists_new_does_not(self, temp_db_and_vaults):
        """
        Branch 1: vaults/{sanitized_name}/ exists, vaults/{id}/ does NOT exist.
        Expected: old_path.rename(new_path)
        """
        data = temp_db_and_vaults

        # Insert vault with id=1, name="Test Vault"
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test Vault')")
        conn.commit()
        conn.close()

        # Create vaults/Test_Vault/ with a file
        old_path = data["vaults_dir"] / "Test_Vault"
        old_path.mkdir(parents=True, exist_ok=True)
        (old_path / "test_file.txt").write_text("test content")

        # Ensure vaults/1/ does NOT exist
        new_path = data["vaults_dir"] / "1"
        assert not new_path.exists()

        # Run migration
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: vaults/1/ now exists and contains the file
        assert new_path.exists()
        assert (new_path / "test_file.txt").exists()
        assert (new_path / "test_file.txt").read_text() == "test content"

        # VERIFY: vaults/Test_Vault/ no longer exists
        assert not old_path.exists()

    def test_branch_2_both_exist_merge(self, temp_db_and_vaults):
        """
        Branch 2: Both vaults/{sanitized_name}/ and vaults/{id}/ exist.
        Expected: copytree with dirs_exist_ok=True, then rmtree old.
        """
        data = temp_db_and_vaults

        # Insert vault with id=2, name="My Docs"
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (2, 'My Docs')")
        conn.commit()
        conn.close()

        # Create vaults/My_Docs/ with files
        old_path = data["vaults_dir"] / "My_Docs"
        old_path.mkdir(parents=True, exist_ok=True)
        (old_path / "old_file.txt").write_text("old content")
        (old_path / "shared.txt").write_text("old version")

        # Create vaults/2/ with different files
        new_path = data["vaults_dir"] / "2"
        new_path.mkdir(parents=True, exist_ok=True)
        (new_path / "new_file.txt").write_text("new content")
        (new_path / "shared.txt").write_text("new version")

        # Run migration
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: vaults/2/ contains merged contents
        assert new_path.exists()
        assert (new_path / "new_file.txt").exists()  # original new file
        assert (new_path / "old_file.txt").exists()  # copied from old
        assert (new_path / "old_file.txt").read_text() == "old content"

        # VERIFY: vaults/My_Docs/ no longer exists
        assert not old_path.exists()

    def test_branch_3_only_new_exists(self, temp_db_and_vaults):
        """
        Branch 3: Only vaults/{id}/ exists, vaults/{sanitized_name}/ does NOT.
        Expected: Skip (already migrated).
        """
        data = temp_db_and_vaults

        # Insert vault with id=1, name="Test"
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test')")
        conn.commit()
        conn.close()

        # Create only vaults/1/ (already migrated)
        new_path = data["vaults_dir"] / "1"
        new_path.mkdir(parents=True, exist_ok=True)
        (new_path / "existing.txt").write_text("existing content")

        # Ensure vaults/Test/ does NOT exist
        old_path = data["vaults_dir"] / "Test"
        assert not old_path.exists()

        # Run migration
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: vaults/1/ unchanged
        assert new_path.exists()
        assert (new_path / "existing.txt").read_text() == "existing content"

        # VERIFY: No old directory was created
        assert not old_path.exists()

    def test_branch_4_neither_exists(self, temp_db_and_vaults):
        """
        Branch 4: Neither vaults/{sanitized_name}/ nor vaults/{id}/ exist.
        Expected: Skip (nothing to do).
        """
        data = temp_db_and_vaults

        # Insert vault with id=99, name="NonExistent"
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (99, 'NonExistent')")
        conn.commit()
        conn.close()

        # Don't create any directories

        # Run migration - should not raise any errors
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: No directories were created
        assert not (data["vaults_dir"] / "NonExistent").exists()
        assert not (data["vaults_dir"] / "99").exists()

    def test_idempotency(self, temp_db_and_vaults):
        """
        Test idempotency: running migration twice should not cause errors.
        """
        data = temp_db_and_vaults

        # Insert vault
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test Vault')")
        conn.commit()
        conn.close()

        # Create old directory structure
        old_path = data["vaults_dir"] / "Test_Vault"
        old_path.mkdir(parents=True, exist_ok=True)
        (old_path / "file.txt").write_text("content")

        # First migration
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # Second migration (should be no-op since already migrated)
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))  # Should not raise

        # VERIFY: Files still in correct location
        new_path = data["vaults_dir"] / "1"
        assert new_path.exists()
        assert (new_path / "file.txt").read_text() == "content"

    def test_multiple_vaults_mixed_states(self, temp_db_and_vaults):
        """
        Test multiple vaults with different migration states in one run.
        """
        data = temp_db_and_vaults

        # Insert multiple vaults
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute(
            "INSERT INTO vaults (id, name) VALUES (1, 'First Vault')"
        )  # Will be branch 1
        conn.execute(
            "INSERT INTO vaults (id, name) VALUES (2, 'Second Vault')"
        )  # Will be branch 3
        conn.execute(
            "INSERT INTO vaults (id, name) VALUES (3, 'Third Vault')"
        )  # Will be branch 4
        conn.commit()
        conn.close()

        # Vault 1: old exists, new doesn't (branch 1)
        old_1 = data["vaults_dir"] / "First_Vault"
        old_1.mkdir(parents=True, exist_ok=True)
        (old_1 / "v1.txt").write_text("vault 1")

        # Vault 2: only new exists (branch 3)
        new_2 = data["vaults_dir"] / "2"
        new_2.mkdir(parents=True, exist_ok=True)
        (new_2 / "v2.txt").write_text("vault 2")

        # Vault 3: neither exists (branch 4)

        # Run migration
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: Vault 1 migrated
        assert (data["vaults_dir"] / "1" / "v1.txt").exists()
        assert not old_1.exists()

        # VERIFY: Vault 2 unchanged
        assert (data["vaults_dir"] / "2" / "v2.txt").read_text() == "vault 2"

        # VERIFY: Vault 3 still doesn't exist
        assert not (data["vaults_dir"] / "3").exists()
        assert not (data["vaults_dir"] / "Third_Vault").exists()

    def test_sanitization_of_vault_names(self, temp_db_and_vaults):
        """
        Test that vault names with special characters are properly sanitized.
        """
        data = temp_db_and_vaults

        # Insert vault with special characters
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test & Files!')")
        conn.commit()
        conn.close()

        # Create directory with sanitized name (spaces and special chars become _)
        old_path = data["vaults_dir"] / "Test___Files_"
        old_path.mkdir(parents=True, exist_ok=True)
        (old_path / "doc.txt").write_text("content")

        # Run migration
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: Migration succeeded
        new_path = data["vaults_dir"] / "1"
        assert new_path.exists()
        assert (new_path / "doc.txt").read_text() == "content"
        assert not old_path.exists()

    def test_vaults_dir_not_exists(self, tmp_path):
        """
        Test that migration handles case where vaults_dir doesn't exist.
        """
        # Create DB but NO vaults directory
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE vaults (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test')")
        conn.commit()
        conn.close()

        # vaults_dir that doesn't exist
        non_existent_dir = tmp_path / "nonexistent"

        # Run migration - should not raise
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = non_existent_dir
            migrate_vault_paths(str(db_path))  # Should handle gracefully

    def test_nested_directory_structure(self, temp_db_and_vaults):
        """
        Test migration handles nested directory structures correctly.
        """
        data = temp_db_and_vaults

        # Insert vault
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Nested')")
        conn.commit()
        conn.close()

        # Create old path with nested structure
        old_path = data["vaults_dir"] / "Nested"
        old_path.mkdir(parents=True, exist_ok=True)
        (old_path / "file1.txt").write_text("root file")
        nested_dir = old_path / "subdir"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / "file2.txt").write_text("nested file")

        # Run migration
        with patch("app.models.database.settings") as mock_settings:
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: Nested structure preserved
        new_path = data["vaults_dir"] / "1"
        assert (new_path / "file1.txt").exists()
        assert (new_path / "subdir" / "file2.txt").exists()
        assert (new_path / "subdir" / "file2.txt").read_text() == "nested file"

    def test_error_handling_continues_with_other_vaults(self, temp_db_and_vaults, caplog):
        """
        Test that migration continues with other vaults when one fails.
        """
        data = temp_db_and_vaults

        # Insert two vaults
        conn = sqlite3.connect(str(data["db_path"]))
        conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Good Vault')")
        conn.execute("INSERT INTO vaults (id, name) VALUES (2, 'Another')")
        conn.commit()
        conn.close()

        # Create first vault directory
        old_1 = data["vaults_dir"] / "Good_Vault"
        old_1.mkdir(parents=True, exist_ok=True)
        (old_1 / "file.txt").write_text("good vault")

        # Create second vault directory
        old_2 = data["vaults_dir"] / "Another"
        old_2.mkdir(parents=True, exist_ok=True)
        (old_2 / "file2.txt").write_text("another vault")

        original_rename = Path.rename

        def fail_first_vault_rename(path: Path, target: Path):
            if path == old_1:
                raise OSError("simulated rename failure")
            return original_rename(path, target)

        # Run migration
        caplog.set_level("WARNING", logger="app.models.database")
        with patch("app.models.database.settings") as mock_settings, patch.object(
            Path, "rename", fail_first_vault_rename
        ):
            mock_settings.vaults_dir = data["vaults_dir"]
            migrate_vault_paths(str(data["db_path"]))

        # VERIFY: Failed vault stayed put, the next vault still migrated, and the error was logged.
        assert (data["vaults_dir"] / "Good_Vault" / "file.txt").exists()
        assert not (data["vaults_dir"] / "1" / "file.txt").exists()
        assert (data["vaults_dir"] / "2" / "file2.txt").exists()
        assert "Failed to migrate vault 'Good Vault' (ID 1)" in caplog.text
        assert "simulated rename failure" in caplog.text
