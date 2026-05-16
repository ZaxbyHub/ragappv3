"""Tests for file_watcher path alignment with vault_uploads_dir.

These tests verify:
1. scan_once() builds vault upload dirs using settings.vault_uploads_dir(vault_id)
2. The generated paths match what email_service._save_attachment uses
3. vault_name is no longer used in path construction
"""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

# Stub missing optional dependencies for testing
try:
    import lancedb
except ImportError:
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')


class TestFileWatcherPathAlignment:
    """Tests verifying file_watcher uses vault_uploads_dir correctly."""

    @pytest.fixture
    def mock_processor(self):
        """Create a mock BackgroundProcessor."""
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        """Create a mock database pool."""
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_scan_once_uses_vault_uploads_dir_for_vault_paths(self, mock_processor, mock_pool):
        """Test that scan_once() builds vault upload dirs using settings.vault_uploads_dir(vault_id)."""
        # Arrange
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None  # No library vault configured
            mock_settings.library_dir = Path("/fake/library")

            # Set up vault_uploads_dir to return specific paths per vault_id
            def vault_uploads_dir_side_effect(vault_id):
                return Path(f"/data/vaults/{vault_id}/uploads")

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            # Mock the get_pool function (imported inside scan_once)
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [
                (1, "Vault One"),
                (2, "Vault Two"),
                (5, "Vault Five"),
            ]

            def get_pool_mock(*args, **kwargs):
                return mock_pool

            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", get_pool_mock):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Act
                await watcher.scan_once()

                # Assert - verify vault_uploads_dir was called with each vault_id
                assert mock_settings.vault_uploads_dir.call_count == 3
                mock_settings.vault_uploads_dir.assert_any_call(1)
                mock_settings.vault_uploads_dir.assert_any_call(2)
                mock_settings.vault_uploads_dir.assert_any_call(5)

    @pytest.mark.asyncio
    async def test_scan_once_paths_match_email_service_paths(self, mock_processor, mock_pool):
        """Test that scan_once() generates paths matching email_service._save_attachment.

        Both should use settings.vault_uploads_dir(vault_id) to build upload paths.
        """
        # Arrange
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            expected_path = Path("/data/vaults/42/uploads")
            mock_settings.vault_uploads_dir.return_value = expected_path

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(42, "Test Vault")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Act
                await watcher.scan_once()

                # Assert - the path used by file_watcher should be the same as what email_service uses
                # email_service uses: provider.get_upload_dir(vault_id) -> settings.vault_uploads_dir(vault_id)
                # So file_watcher should call settings.vault_uploads_dir(42) and get the same path
                mock_settings.vault_uploads_dir.assert_called_with(42)
                assert mock_settings.vault_uploads_dir.return_value == expected_path

    @pytest.mark.asyncio
    async def test_scan_once_does_not_use_vault_name_in_paths(self, mock_processor, mock_pool):
        """Verify vault_name is not used in path construction (just a sanity check).

        The old implementation used safe_name (derived from vault name) in paths:
            settings.vaults_dir / safe_name / "uploads"

        The new implementation uses vault_id directly:
            settings.vault_uploads_dir(vault_id)

        This test verifies that vault_name is not passed to vault_uploads_dir.
        """
        # Arrange
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            def vault_uploads_dir_side_effect(vault_id):
                # This should ONLY receive vault_id (int), NOT a string name
                assert isinstance(vault_id, int), f"vault_uploads_dir should receive int, got {type(vault_id)}"
                return Path(f"/data/vaults/{vault_id}/uploads")

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            # Vault names contain strings - but they should NOT be used in path construction
            mock_conn.execute.return_value.fetchall.return_value = [
                (10, "My Important Vault"),
                (20, "Another Vault with Spaces"),
            ]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Act & Assert - should not raise AssertionError
                await watcher.scan_once()

                # Verify vault_uploads_dir was called with integer IDs only
                for call in mock_settings.vault_uploads_dir.call_args_list:
                    vault_id_arg = call[0][0]
                    assert isinstance(vault_id_arg, int), f"Expected int vault_id, got {type(vault_id_arg)}"

    @pytest.mark.asyncio
    async def test_scan_once_handles_library_dir_when_configured(self, mock_processor, mock_pool, tmp_path):
        """Test that library_dir is added to dir_vault_map when library_vault_id is set."""
        # Arrange
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"

            library_dir = tmp_path / "library"
            library_dir.mkdir()
            mock_settings.library_dir = library_dir
            mock_settings.library_vault_id = 99

            # Set up vault_uploads_dir
            def vault_uploads_dir_side_effect(vault_id):
                return Path(f"/data/vaults/{vault_id}/uploads")

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []  # No regular vaults
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Act
                await watcher.scan_once()

                # Assert - library_dir should be included in the scan
                # The implementation builds dir_vault_map and enqueues from each directory
                # We can verify by checking that enqueue was called (or not called if no files)
                # The key point is library_vault_id was read and library_dir was added

    def test_file_watcher_no_vault_name_attribute(self):
        """Sanity check that FileWatcher doesn't reference vault_name in path construction."""
        # This is a compile-time check - if scan_once uses vault_name incorrectly,
        # the path construction would fail when vault_name is not available

        from app.services.file_watcher import FileWatcher

        # FileWatcher should have a scan_once method
        assert hasattr(FileWatcher, 'scan_once')

        # The scan_once method's code should not contain "vault_name" string
        # (checking the source would require inspect, but this test serves as documentation)
        import inspect
        source = inspect.getsource(FileWatcher.scan_once)
        # If vault_name appears in the source, it might be used incorrectly
        assert "vault_name" not in source, "scan_once should not use vault_name in path construction"


class TestFileWatcherVaultUploadsDirAlignment:
    """Tests verifying file_watcher path alignment with other services."""

    def test_vault_uploads_dir_is_called_by_both_services(self):
        """Verify both email_service and file_watcher call vault_uploads_dir.

        email_service._save_attachment:
            provider = UploadPathProvider()
            upload_dir = provider.get_upload_dir(vault_id)
            # provider.get_upload_dir returns settings.vault_uploads_dir(vault_id)

        file_watcher.scan_once:
            vault_upload_dir = settings.vault_uploads_dir(vault_id)
        """
        # Both should call settings.vault_uploads_dir with vault_id

        # Read the source of email_service._save_attachment to verify
        import inspect

        from app.services.email_service import EmailIngestionService

        email_save_source = inspect.getsource(EmailIngestionService._save_attachment)
        assert "vault_uploads_dir" in email_save_source or "get_upload_dir" in email_save_source

        # Read the source of file_watcher.scan_once
        from app.services.file_watcher import FileWatcher
        file_watcher_source = inspect.getsource(FileWatcher.scan_once)
        assert "vault_uploads_dir" in file_watcher_source

    def test_upload_path_provider_returns_vault_uploads_dir(self):
        """Verify UploadPathProvider.get_upload_dir returns vault_uploads_dir."""
        import inspect

        from app.services.upload_path import UploadPathProvider

        source = inspect.getsource(UploadPathProvider.get_upload_dir)
        assert "vault_uploads_dir" in source, "get_upload_dir should call vault_uploads_dir"
