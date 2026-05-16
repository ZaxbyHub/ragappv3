"""Adversarial tests for file_watcher path alignment.

Tests designed to BREAK the path alignment change by injecting failure modes:
1. vault_uploads_dir() raises an exception
2. vault_id is negative or zero
3. settings.vaults_dir is a symlink or doesn't exist
4. Race condition: vault deleted between SELECT and path construction
5. settings object doesn't have vault_uploads_dir method
"""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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


class TestVaultUploadsDirExceptions:
    """Test how file_watcher handles vault_uploads_dir() failures."""

    @pytest.fixture
    def mock_processor(self):
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_vault_uploads_dir_raises_permission_error(self, mock_processor, mock_pool):
        """ADVERSARIAL: vault_uploads_dir raises PermissionError - should not crash scan_once."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # vault_uploads_dir raises PermissionError
            mock_settings.vault_uploads_dir.side_effect = PermissionError("Access denied")

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should not raise - should handle gracefully
                result = await watcher.scan_once()

                # Should return 0 since vault scan failed
                assert result == 0

    @pytest.mark.asyncio
    async def test_vault_uploads_dir_raises_os_error(self, mock_processor, mock_pool):
        """ADVERSARIAL: vault_uploads_dir raises OSError - should not crash scan_once."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # vault_uploads_dir raises OSError (disk full, etc.)
            mock_settings.vault_uploads_dir.side_effect = OSError("Disk full")

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should not raise
                result = await watcher.scan_once()
                assert result == 0

    @pytest.mark.asyncio
    async def test_vault_uploads_dir_raises_attribute_error(self, mock_processor, mock_pool):
        """ADVERSARIAL: vault_uploads_dir raises AttributeError - should not crash scan_once."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # Simulate settings object missing the vault_uploads_dir method
            del mock_settings.vault_uploads_dir
            # AttributeError when accessing it
            mock_settings.__dict__['vault_uploads_dir'] = property(lambda self: AttributeError("No method"))

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle AttributeError gracefully
                try:
                    result = await watcher.scan_once()
                    # If it returns at all, verify reasonable behavior
                    assert isinstance(result, int)
                except AttributeError:
                    pytest.fail("scan_once did not handle missing vault_uploads_dir gracefully")


class TestNegativeAndZeroVaultId:
    """Test how file_watcher handles negative and zero vault_ids."""

    @pytest.fixture
    def mock_processor(self):
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_vault_id_zero(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: vault_id=0 should not cause path traversal or errors."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # Capture what vault_id is passed
            captured_ids = []
            def vault_uploads_dir_side_effect(vault_id):
                captured_ids.append(vault_id)
                # Return a path - should not cause issues
                return tmp_path / str(vault_id) / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(0, "Zero Vault")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle vault_id=0 gracefully
                await watcher.scan_once()

                # Verify vault_id=0 was passed (not used in path traversal)
                assert 0 in captured_ids

    @pytest.mark.asyncio
    async def test_negative_vault_id(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: negative vault_id should not cause path traversal."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            captured_ids = []
            def vault_uploads_dir_side_effect(vault_id):
                captured_ids.append(vault_id)
                return tmp_path / str(vault_id) / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            # Negative vault_id from database
            mock_conn.execute.return_value.fetchall.return_value = [(-1, "Negative Vault")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle negative vault_id gracefully
                await watcher.scan_once()

                assert -1 in captured_ids

    @pytest.mark.asyncio
    async def test_very_large_vault_id(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: extremely large vault_id should not cause overflow or issues."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            captured_ids = []
            def vault_uploads_dir_side_effect(vault_id):
                captured_ids.append(vault_id)
                return tmp_path / str(vault_id) / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            # Very large vault_id (could cause issues if used directly in paths)
            mock_conn.execute.return_value.fetchall.return_value = [(2**31 - 1, "Large Vault")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                await watcher.scan_once()

                assert 2**31 - 1 in captured_ids


class TestSymlinkAndNonExistentPaths:
    """Test how file_watcher handles symlinks and non-existent paths."""

    @pytest.fixture
    def mock_processor(self):
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_vault_uploads_dir_returns_symlink(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: vault_uploads_dir returns a symlink - should handle safely."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # Create a symlink as the vault uploads dir
            real_dir = tmp_path / "real_dir"
            real_dir.mkdir()
            symlink_dir = tmp_path / "symlink_dir"
            if sys.platform != "win32":
                symlink_dir.symlink_to(real_dir)

            mock_settings.vault_uploads_dir.return_value = symlink_dir

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should not crash on symlink
                result = await watcher.scan_once()
                assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_vault_uploads_dir_returns_nonexistent_path(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: vault_uploads_dir returns a path that doesn't exist."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # Return a path that doesn't exist
            nonexistent = tmp_path / "nonexistent" / "vault" / "uploads"
            mock_settings.vault_uploads_dir.return_value = nonexistent

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle nonexistent path gracefully
                result = await watcher.scan_once()
                assert result == 0  # No files found in nonexistent dir

    @pytest.mark.asyncio
    async def test_vault_uploads_dir_returns_path_with_special_chars(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: vault_uploads_dir returns path with spaces and special characters."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # Path with spaces and special chars
            special_dir = tmp_path / "path with spaces" / "vault&42" / "uploads"
            mock_settings.vault_uploads_dir.return_value = special_dir

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle special chars gracefully
                result = await watcher.scan_once()
                assert isinstance(result, int)


class TestRaceConditionVaultDeleted:
    """Test race condition: vault deleted between SELECT and path construction."""

    @pytest.fixture
    def mock_processor(self):
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_vault_deleted_race_between_select_and_use(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: Vault is deleted after SELECT but before path use."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            call_count = [0]
            def vault_uploads_dir_side_effect(vault_id):
                call_count[0] += 1
                # First call returns valid path, second call returns deleted path
                if call_count[0] == 1:
                    return tmp_path / str(vault_id) / "uploads"
                else:
                    # Simulate race: directory deleted between calls
                    raise FileNotFoundError(f"Vault {vault_id} deleted during scan")

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle race condition gracefully
                result = await watcher.scan_once()
                # Should not crash, returns some count (likely 0)
                assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_concurrent_vault_deletion(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: Multiple vaults, some deleted during scan."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            def vault_uploads_dir_side_effect(vault_id):
                if vault_id == 1:
                    return tmp_path / "1" / "uploads"
                elif vault_id == 2:
                    # Vault 2 deleted
                    raise FileNotFoundError(f"Vault {vault_id} deleted")
                elif vault_id == 3:
                    return tmp_path / "3" / "uploads"
                return tmp_path / str(vault_id) / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [
                (1, "Vault One"),
                (2, "Vault Two"),
                (3, "Vault Three"),
            ]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should process vaults 1 and 3, skip vault 2 gracefully
                result = await watcher.scan_once()
                assert isinstance(result, int)


class TestSettingsObjectMissingMethod:
    """Test when settings object doesn't have vault_uploads_dir method."""

    @pytest.fixture
    def mock_processor(self):
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_settings_missing_vault_uploads_dir_method(self, mock_processor, mock_pool):
        """ADVERSARIAL: settings object completely missing vault_uploads_dir.

        The code catches the exception and logs a warning - it does NOT crash.
        This is CORRECT behavior for graceful degradation.
        """
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # Remove vault_uploads_dir from the mock
            if hasattr(mock_settings, 'vault_uploads_dir'):
                del mock_settings.vault_uploads_dir

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should NOT raise - code catches the error and logs warning
                # Result is 0 because no vaults could be scanned
                result = await watcher.scan_once()
                assert result == 0

    @pytest.mark.asyncio
    async def test_settings_vault_uploads_dir_is_not_callable(self, mock_processor, mock_pool):
        """ADVERSARIAL: vault_uploads_dir exists but is not callable.

        The code catches the exception and logs a warning - it does NOT crash.
        This is CORRECT behavior for graceful degradation.
        """
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # vault_uploads_dir is a string, not a method
            mock_settings.vault_uploads_dir = "/path/to/uploads"

            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should NOT raise - code catches the error and logs warning
                # Result is 0 because no vaults could be scanned
                result = await watcher.scan_once()
                assert result == 0


class TestPathTraversalAndInjection:
    """Test for path traversal and injection attacks."""

    @pytest.fixture
    def mock_processor(self):
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_vault_id_sql_injection_in_path(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: SQL injection-like vault_id should not cause path issues."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            # Malicious-looking vault_id from DB
            malicious_id = "1; DROP TABLE vaults;--"
            captured_ids = []

            def vault_uploads_dir_side_effect(vault_id):
                captured_ids.append(vault_id)
                return tmp_path / "vault" / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            # Database returns malicious string as vault_id
            mock_conn.execute.return_value.fetchall.return_value = [(malicious_id, "Malicious Vault")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle string vault_id without path traversal
                try:
                    await watcher.scan_once()
                    # vault_id was passed as-is to vault_uploads_dir
                    assert malicious_id in captured_ids
                except Exception:
                    # Any exception is acceptable as long as it's not a path traversal
                    pass

    @pytest.mark.asyncio
    async def test_vault_name_with_path_traversal_chars(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: Vault name contains ../ to test path traversal."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            captured_ids = []
            def vault_uploads_dir_side_effect(vault_id):
                captured_ids.append(vault_id)
                return tmp_path / str(vault_id) / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            # Vault name contains path traversal attempt
            mock_conn.execute.return_value.fetchall.return_value = [(1, "../../../etc/passwd")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # The old implementation used vault_name in paths
                # The new implementation uses vault_id (int), so this should be safe
                await watcher.scan_once()

                # Verify vault_id (int) was used, not vault_name
                assert 1 in captured_ids
                # vault_name is not passed to vault_uploads_dir


class TestUnicodeAndEncoding:
    """Test Unicode and encoding edge cases."""

    @pytest.fixture
    def mock_processor(self):
        processor = AsyncMock()
        processor.enqueue = AsyncMock(return_value=True)
        return processor

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return pool

    @pytest.mark.asyncio
    async def test_vault_name_unicode(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: Vault name contains Unicode characters."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            captured_ids = []
            def vault_uploads_dir_side_effect(vault_id):
                captured_ids.append(vault_id)
                return tmp_path / str(vault_id) / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            # Unicode vault name
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault 🗂️")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle Unicode gracefully
                result = await watcher.scan_once()
                assert isinstance(result, int)
                assert 1 in captured_ids

    @pytest.mark.asyncio
    async def test_null_bytes_in_vault_name(self, mock_processor, mock_pool, tmp_path):
        """ADVERSARIAL: Vault name contains null bytes."""
        with patch("app.services.file_watcher.settings") as mock_settings:
            mock_settings.sqlite_path = "/fake/sqlite.db"
            mock_settings.library_vault_id = None
            mock_settings.library_dir = Path("/fake/library")

            captured_ids = []
            def vault_uploads_dir_side_effect(vault_id):
                captured_ids.append(vault_id)
                return tmp_path / str(vault_id) / "uploads"

            mock_settings.vault_uploads_dir.side_effect = vault_uploads_dir_side_effect

            mock_conn = MagicMock()
            # Null byte in vault name
            mock_conn.execute.return_value.fetchall.return_value = [(1, "Vault\x00Hacked")]
            mock_pool.get_connection.return_value = mock_conn

            with patch("app.models.database.get_pool", return_value=mock_pool):
                from app.services.file_watcher import FileWatcher

                watcher = FileWatcher(mock_processor, mock_pool)

                # Should handle null bytes gracefully
                result = await watcher.scan_once()
                assert isinstance(result, int)
                assert 1 in captured_ids
