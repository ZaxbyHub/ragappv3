"""K04 mixed-outcome scan checks (issue #767 AC6): truthful counts.

Companion to tests/test_k04_scan_counts.py for the scenarios a single
"everything is known" directory cannot cover:

  (a) a MIXED directory (one known file + one new file) reports
      ``files_examined == 2`` / ``files_enqueued == 1`` through the route;
  (b) a candidate failing the structural validation screen
      (``_validate_scan_candidate``) is EXAMINED but not enqueued and is NOT
      reported as an error — a per-file decision, not a directory failure;
  (c) a vault whose ``settings.vault_uploads_dir`` lookup raises alongside a
      healthy vault directory: the healthy directory is still scanned and
      counted, and exactly one ``errors`` entry names the failure.

Fixture patterns mirror tests/test_k04_scan_counts.py (the async
``get_connection_async`` vaults pool behind ``app.models.database.get_pool``,
the injected sync pool for the ``files`` query, and the ``file_watcher``
settings patch), itself modeled on tests/test_file_watcher_adversarial.py.
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


def make_vaults_pool(vault_rows):
    """The async pool scan_once looks up via app.models.database.get_pool."""
    async_pool = MagicMock()
    vault_conn = MagicMock()
    vault_conn.execute.return_value.fetchall.return_value = vault_rows
    async_pool.get_connection_async = AsyncMock(return_value=vault_conn)
    async_pool.release_connection = MagicMock()
    return async_pool


def make_files_pool(known_paths):
    """The injected sync pool answering the files query in _find_new_files."""
    files_pool = MagicMock()
    files_conn = MagicMock()
    files_conn.execute.return_value.fetchall.return_value = [
        {"file_path": str(path)} for path in known_paths
    ]
    files_pool.get_connection = MagicMock(return_value=files_conn)
    files_pool.release_connection = MagicMock()
    return files_pool


def make_background_processor():
    processor = MagicMock()
    processor.is_running = True
    processor.enqueue = AsyncMock()
    return processor


async def test_mixed_directory_reports_examined_and_enqueued_separately(tmp_path):
    """One known file + one new file: examined 2, enqueued 1, no errors."""
    from app.api.routes.documents import scan_directories

    uploads_dir = tmp_path / "vault-uploads"
    uploads_dir.mkdir()
    known = uploads_dir / "alpha.txt"
    known.write_text("already indexed", encoding="utf-8")
    fresh = uploads_dir / "beta.txt"
    fresh.write_text("brand new", encoding="utf-8")

    async_pool = make_vaults_pool([(1, "Vault One")])
    files_pool = make_files_pool([known.resolve()])
    processor = make_background_processor()

    with patch("app.services.file_watcher.settings") as mock_settings:
        mock_settings.sqlite_path = "/fake/sqlite.db"
        mock_settings.library_vault_id = None
        mock_settings.library_dir = tmp_path / "library"
        mock_settings.vault_uploads_dir = MagicMock(return_value=uploads_dir)

        with patch("app.models.database.get_pool", return_value=async_pool):
            response = await scan_directories(
                request=MagicMock(),
                background_processor=processor,
                db_pool=files_pool,
                user=ADMIN_USER,
                _csrf_token="test-csrf-token",
            )

    # Both candidates were checked against the database; only the new one
    # was enqueued. At base ``scanned`` mirrored the enqueued count (1).
    assert response.scanned == 2
    assert response.files_enqueued == 1
    assert response.added == 1
    assert response.status == "success"
    assert response.errors == []
    # Exactly the new file reached the processor, under its vault id.
    assert processor.enqueue.await_count == 1
    enqueued_path, enqueued_kwargs = processor.enqueue.await_args[0][0], processor.enqueue.await_args.kwargs
    assert enqueued_path == str(fresh.resolve())
    assert enqueued_kwargs == {"vault_id": 1}


async def test_validation_rejected_candidate_is_examined_not_an_error(
    tmp_path, monkeypatch
):
    """A file failing the structural screen: examined 1, enqueued 0, no error.

    The screen rejecting a file is a per-file decision (logged, skipped) —
    it must not be counted as a directory error in ``ScanResult.errors``.
    """
    from app.services.file_watcher import FileWatcher, ScanResult

    uploads_dir = tmp_path / "vault-uploads"
    uploads_dir.mkdir()
    rejected = uploads_dir / "broken.pdf"
    rejected.write_text("structurally invalid payload", encoding="utf-8")

    async_pool = make_vaults_pool([(1, "Vault One")])
    files_pool = make_files_pool([])  # nothing known: the file is new
    processor = make_background_processor()

    monkeypatch.setattr(
        "app.services.file_watcher._validate_scan_candidate",
        lambda file_path: (False, "header mismatch"),
    )

    watcher = FileWatcher(processor, pool=files_pool)
    with patch("app.services.file_watcher.settings") as mock_settings:
        mock_settings.sqlite_path = "/fake/sqlite.db"
        mock_settings.library_vault_id = None
        mock_settings.library_dir = tmp_path / "library"
        mock_settings.vault_uploads_dir = MagicMock(return_value=uploads_dir)

        with patch("app.models.database.get_pool", return_value=async_pool):
            result = await watcher.scan_once()

    assert result == ScanResult(files_examined=1, files_enqueued=0, errors=[])
    assert processor.enqueue.await_count == 0


async def test_failing_vault_directory_keeps_healthy_vault_scanned(tmp_path):
    """One vault's uploads-dir lookup raises: healthy vault still counted.

    The vaults row processed BEFORE the failure stays in the scan map (the
    collection loop populates the dict incrementally), so its directory is
    still scanned and its files counted; the failure is reported exactly
    once in ``errors`` naming the cause.
    """
    from app.api.routes.documents import scan_directories

    healthy_uploads = tmp_path / "vault-two-uploads"
    healthy_uploads.mkdir()
    fresh = healthy_uploads / "gamma.txt"
    fresh.write_text("healthy vault new file", encoding="utf-8")

    # Healthy vault (id 2) FIRST: its directory lands in the scan map before
    # the broken vault's (id 1) lookup raises.
    async_pool = make_vaults_pool([(2, "Healthy Vault"), (1, "Broken Vault")])
    files_pool = make_files_pool([])
    processor = make_background_processor()

    def vault_uploads_side_effect(vault_id):
        if vault_id == 1:
            raise RuntimeError("vault 1 uploads dir unavailable")
        return healthy_uploads

    with patch("app.services.file_watcher.settings") as mock_settings:
        mock_settings.sqlite_path = "/fake/sqlite.db"
        mock_settings.library_vault_id = None
        mock_settings.library_dir = tmp_path / "library"
        mock_settings.vault_uploads_dir = MagicMock(
            side_effect=vault_uploads_side_effect
        )

        with patch("app.models.database.get_pool", return_value=async_pool):
            response = await scan_directories(
                request=MagicMock(),
                background_processor=processor,
                db_pool=files_pool,
                user=ADMIN_USER,
                _csrf_token="test-csrf-token",
            )

    # The healthy vault's new file was examined and enqueued...
    assert response.scanned == 1
    assert response.files_enqueued == 1
    assert processor.enqueue.await_count == 1
    assert processor.enqueue.await_args.kwargs == {"vault_id": 2}
    # ...and the broken vault's failure is reported once, naming the cause.
    assert len(response.errors) == 1
    assert "vault directories" in response.errors[0]
    assert "vault 1 uploads dir unavailable" in response.errors[0]
