"""K04 acceptance check (issue #767 AC6): truthful scan counts.

WHEN directories are scanned and every file on disk is already in the
database, POST /api/documents/scan must report the number of files examined
in ``scanned`` rather than the enqueued count (0). RED at base (the route
sets ``scanned = files_enqueued``), GREEN after the fix.

Fixture patterns mirror tests/test_file_watcher_adversarial.py (the async
``get_connection_async`` vaults pool plus the injected sync pool for the
``files`` query) and the admin user dict shape of
tests/test_scan_directories_auth_change.py. The route function is invoked
directly with the background processor mocked as running, so no ASGI or
auth machinery is involved.
"""

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

# Stub missing optional dependencies for testing
try:
    import lancedb
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")


ADMIN_USER = {
    "id": 2,
    "username": "admin1",
    "full_name": "Admin One",
    "role": "admin",
}


async def test_scan_reports_files_examined_not_only_enqueued(tmp_path):
    """Two known files on disk, zero new: ``scanned`` must be 2, not 0.

    RED at base (``scanned`` mirrors ``files_enqueued`` == 0), GREEN after
    the fix.
    """
    from app.api.routes.documents import scan_directories

    uploads_dir = tmp_path / "vault-uploads"
    uploads_dir.mkdir()
    file_one = uploads_dir / "alpha.txt"
    file_one.write_text("alpha contents", encoding="utf-8")
    file_two = uploads_dir / "beta.txt"
    file_two.write_text("beta contents", encoding="utf-8")
    resolved_one = file_one.resolve()
    resolved_two = file_two.resolve()

    # The pool scan_once looks up via app.models.database.get_pool answers
    # the vaults query (async surface, as in test_file_watcher_adversarial).
    async_pool = MagicMock()
    vault_conn = MagicMock()
    vault_conn.execute.return_value.fetchall.return_value = [(1, "Vault One")]
    async_pool.get_connection_async = AsyncMock(return_value=vault_conn)
    async_pool.release_connection = MagicMock()

    # The injected db_pool answers the files query in _find_new_files
    # (sync surface): every file on disk is already registered, both in
    # resolved form.
    files_pool = MagicMock()
    files_conn = MagicMock()
    files_conn.execute.return_value.fetchall.return_value = [
        {"file_path": str(resolved_one)},
        {"file_path": str(resolved_two)},
    ]
    files_pool.get_connection = MagicMock(return_value=files_conn)
    files_pool.release_connection = MagicMock()

    background_processor = MagicMock()
    background_processor.is_running = True
    background_processor.enqueue = AsyncMock()

    with patch("app.services.file_watcher.settings") as mock_settings:
        mock_settings.sqlite_path = "/fake/sqlite.db"
        mock_settings.library_vault_id = None
        mock_settings.library_dir = tmp_path / "library"
        mock_settings.vault_uploads_dir = MagicMock(return_value=uploads_dir)

        with patch("app.models.database.get_pool", return_value=async_pool):
            response = await scan_directories(
                request=MagicMock(),
                background_processor=background_processor,
                db_pool=files_pool,
                user=ADMIN_USER,
                _csrf_token="test-csrf-token",
            )

    # Discriminating assertion: two files were examined and none were new,
    # so the reported scan count is the number of files examined. At base
    # the response echoes the enqueued count (0) instead.
    assert response.scanned == 2
