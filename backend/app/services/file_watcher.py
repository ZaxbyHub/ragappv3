"""
File watcher service for auto-scanning directories.

Provides FileWatcher class that periodically scans configured directories
for new files and enqueues them for processing via BackgroundProcessor.
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, Set

from ..config import settings
from ..models.database import SQLiteConnectionPool
from .background_tasks import BackgroundProcessor
from .upload_path import UploadPathProvider

logger = logging.getLogger(__name__)


class FileWatcher:
    """
    File watcher for auto-scanning directories for new files.

    Periodically scans settings.uploads_dir and settings.library_dir for files
    not present in the database, enqueuing new files via BackgroundProcessor.
    Respects settings.auto_scan_enabled and settings.auto_scan_interval_minutes.

    Attributes:
        processor: BackgroundProcessor instance for enqueueing new files
        _watching_task: Reference to the watching coroutine
        _running: Boolean indicating if watcher is active
        _shutdown_event: asyncio.Event for graceful shutdown
    """

    def __init__(self, processor: BackgroundProcessor, pool: Optional[SQLiteConnectionPool] = None):
        """
        Initialize the file watcher.

        Args:
            processor: BackgroundProcessor instance for enqueueing files
            pool: Optional SQLiteConnectionPool for database connections
        """
        self.processor = processor
        self.pool = pool
        self._watching_task: Optional[asyncio.Task] = None
        self._running = False
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """
        Start the file watcher loop.

        Begins scanning directories at configured intervals if auto_scan_enabled.
        Safe to call multiple times - will not create duplicate watchers.
        """
        if self._running:
            logger.warning("File watcher is already running")
            return

        if not settings.auto_scan_enabled:
            logger.info("Auto-scan is disabled, file watcher not started")
            return

        self._running = True
        self._shutdown_event.clear()
        self._watching_task = asyncio.create_task(self._watch_loop())
        logger.info("File watcher started")

    async def stop(self) -> None:
        """
        Stop the file watcher gracefully.

        Signals the watcher to shut down and waits for it to complete.
        """
        if not self._running:
            logger.warning("File watcher is not running")
            return

        logger.info("Stopping file watcher...")
        self._shutdown_event.set()

        if self._watching_task:
            try:
                await asyncio.wait_for(self._watching_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Watch task did not stop gracefully, cancelling...")
                self._watching_task.cancel()
                try:
                    await self._watching_task
                except asyncio.CancelledError:
                    pass

        self._running = False
        logger.info("File watcher stopped")

    async def scan_once(self) -> int:
        """
        Perform a single scan of all configured directories.

        Scans settings.uploads_dir and settings.library_dir for files
        not present in the database, enqueuing new files.

        Returns:
            int: Number of new files enqueued for processing
        """
        enqueued_count = 0
        # Scan vault-specific upload directories + library
        UploadPathProvider()
        dir_vault_map: Dict[Path, int] = {}

        # Add each vault's upload directory
        try:
            from app.models.database import get_pool
            pool = get_pool(str(settings.sqlite_path))
            conn = pool.get_connection()
            try:
                vaults = conn.execute("SELECT id, name FROM vaults").fetchall()
                for row in vaults:
                    vault_id = row[0]
                    vault_upload_dir = settings.vault_uploads_dir(vault_id)
                    dir_vault_map[vault_upload_dir] = vault_id
            finally:
                pool.release_connection(conn)
        except Exception as e:
            logger.warning(f"Failed to get vault directories: {e}")

        # Add library_dir only if configured
        if settings.library_vault_id is not None:
            if settings.library_dir.exists():
                dir_vault_map[settings.library_dir] = settings.library_vault_id
            else:
                logger.info("library_vault_id is configured but library directory does not exist, skipping library directory scan")
        else:
            logger.info("library_vault_id is not configured, skipping library directory scan")

        for directory, vault_id in dir_vault_map.items():
            if not directory.exists():
                logger.debug(f"Directory does not exist, skipping: {directory}")
                continue

            try:
                new_files = self._find_new_files(directory)
                for file_path in new_files:
                    await self.processor.enqueue(str(file_path), vault_id=vault_id)
                    enqueued_count += 1
                    logger.info(f"Enqueued new file for processing: {file_path}")
            except Exception as e:
                logger.error(f"Error scanning directory {directory}: {e}")

        if enqueued_count > 0:
            logger.info(f"Scan complete: {enqueued_count} new files enqueued")
        else:
            logger.debug("Scan complete: no new files found")

        return enqueued_count

    def _find_new_files(self, directory: Path) -> Set[Path]:
        """
        Find files in directory that are not in the database.

        Args:
            directory: Path to scan for files

        Returns:
            Set of Path objects for files not in the database
        """
        # Get all files in directory (recursively)
        files_on_disk: Set[Path] = set()
        if directory.exists():
            for file_path in directory.rglob("*"):
                if file_path.is_file():
                    files_on_disk.add(file_path.resolve())

        # Get files from database
        files_in_db: Set[str] = set()
        try:
            if self.pool is None:
                from ..models.database import get_pool
                self.pool = get_pool(str(settings.sqlite_path), max_size=2)
            conn = self.pool.get_connection()
            try:
                cursor = conn.execute(
                    "SELECT file_path FROM files WHERE file_path LIKE ?",
                    (f"{str(directory)}%",)
                )
                for row in cursor.fetchall():
                    files_in_db.add(row["file_path"])
            finally:
                self.pool.release_connection(conn)
        except Exception as e:
            logger.error(f"Error querying database: {e}")
            return set()

        # Find new files (on disk but not in DB)
        new_files: Set[Path] = set()
        for file_path in files_on_disk:
            if str(file_path) not in files_in_db:
                new_files.add(file_path)

        return new_files

    async def _watch_loop(self) -> None:
        """
        Main watch loop that periodically scans directories.

        Continuously scans at configured intervals until shutdown_event is set.
        """
        interval_seconds = settings.auto_scan_interval_minutes * 60

        while not self._shutdown_event.is_set():
            try:
                await self.scan_once()
            except Exception as e:
                logger.error(f"Error during scan: {e}")

            # Wait for next scan interval or shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                # Timeout means interval elapsed, continue to next scan
                pass

    @property
    def is_running(self) -> bool:
        return self._running
