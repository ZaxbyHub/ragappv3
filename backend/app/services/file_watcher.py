"""
File watcher service for auto-scanning directories.

Provides FileWatcher class that periodically scans configured directories
for new files and enqueues them for processing via BackgroundProcessor.
"""

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import Dict, Optional, Set

from ..config import settings
from ..models.database import SQLiteConnectionPool
from .background_tasks import BackgroundProcessor
from .upload_path import UploadPathProvider
from .upload_validation import validate_ingest_candidate

logger = logging.getLogger(__name__)


def _validate_scan_candidate(file_path: Path) -> tuple[bool, Optional[str]]:
    """Thread-context wrapper around the shared structural screen.

    ``validate_ingest_candidate`` performs blocking IO (header + zip central
    directory reads); ``scan_once`` calls this through ``asyncio.to_thread``
    so none of it lands on the event loop.
    """
    return validate_ingest_candidate(file_path)


class FileWatcher:
    """
    File watcher for auto-scanning directories for new files.

    Periodically scans settings.uploads_dir and settings.library_dir for files
    not present in the database, enqueuing new files via BackgroundProcessor.
    Respects settings.auto_scan_enabled and settings.auto_scan_interval_minutes
    — re-evaluated live via ``reconcile()`` after every settings save (issue
    #494 CONFIG-001), not only at startup.

    Attributes:
        processor: BackgroundProcessor instance for enqueueing new files
        _watching_task: Reference to the watching coroutine
        _running: Boolean indicating if watcher is active
        _shutdown_event: asyncio.Event for graceful shutdown
        _wake_event: asyncio.Event used to interrupt the interval wait so a
            saved cadence change applies on the next cycle
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
        # Lifecycle handshake (issue #693 / T1-27-S-06): ``_stopping`` marks an
        # in-progress stop() drain and ``_pending_start`` records a restart
        # requested while a drain was already under way. Both are read and
        # written ONLY on the event loop that owns the watch task (stop(),
        # start(), and _ensure_running all execute there, each in synchronous
        # stretches), so no cross-thread interleaving can drop a restart.
        self._stopping = False
        self._pending_start = False
        self._shutdown_event = asyncio.Event()
        # Wake signal for prompt reconciliation (issue #494 CONFIG-001): a
        # saved auto_scan_interval_minutes change sets this so the watch loop
        # re-reads the interval on its next cycle instead of sleeping out the
        # old cadence.
        self._wake_event = asyncio.Event()
        # The event loop the watch task lives on. ``reconcile`` may be invoked
        # from a threadpool worker (sync FastAPI settings handlers), so
        # lifecycle transitions are marshalled back onto this loop.
        try:
            self._loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

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
        self._loop = asyncio.get_running_loop()
        self._shutdown_event.clear()
        self._watching_task = asyncio.create_task(self._watch_loop())
        logger.info("File watcher started")

    async def stop(self) -> None:
        """
        Stop the file watcher gracefully.

        Signals the watcher to shut down and waits for it to complete. A
        restart requested while the drain was under way (``reconcile``
        during stop, issue #693 / T1-27-S-06) is honored after the drain:
        the watcher ends RUNNING rather than stopping while
        ``auto_scan_enabled`` is True.
        """
        if not self._running:
            logger.warning("File watcher is not running")
            return
        if self._stopping:
            # A second stop racing an in-progress drain must not observe the
            # drained task and clobber ``_running`` after a pending restart.
            logger.debug("File watcher stop already in progress")
            return

        logger.info("Stopping file watcher...")
        self._stopping = True
        try:
            self._shutdown_event.set()
            # Interrupt an in-flight interval wait so shutdown is prompt (the
            # watch loop waits on wake OR shutdown, whichever fires first).
            self._wake_event.set()

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

            if self._pending_start:
                # reconcile(enabled=True) arrived while this drain was under
                # way: honor it now instead of leaving the watcher stopped
                # with auto_scan_enabled True (issue #693 / T1-27-S-06).
                self._pending_start = False
                logger.info("File watcher restart requested during stop; restarting")
                await self.start()
        finally:
            self._stopping = False

    async def _ensure_running(self) -> None:
        """Loop-owned restart decision for ``reconcile(enabled=True)``.

        Runs ON the event loop that owns the watch task (scheduled via
        ``run_coroutine_threadsafe``) so the whole read-decide-write below is
        a synchronous stretch relative to stop()/start(): if a stop() drain
        is under way, record the pending start so stop() restarts when the
        drain completes; if the watcher is stopped, start it; otherwise only
        wake the loop so a changed cadence applies on the next cycle.
        """
        if self._stopping:
            self._pending_start = True
            self._wake_event.set()
            return
        if not self._running:
            await self.start()
            return
        self._wake_event.set()

    def reconcile(self, settings_source=None) -> None:
        """Reconcile the watcher lifecycle with the current auto-scan settings.

        Called from the settings save path (POST/PUT /api/settings) so a saved
        ``auto_scan_enabled`` / ``auto_scan_interval_minutes`` change takes
        effect WITHOUT an app restart (issue #494 CONFIG-001):

          - ``auto_scan_enabled`` False -> stop a running watcher;
          - ``auto_scan_enabled`` True  -> ensure the watcher is running
            (including a restart requested while a stop() drain is still in
            progress, issue #693), and wake a running watcher so a changed
            interval applies on its next cycle (the loop re-reads
            ``settings.auto_scan_interval_minutes`` on every iteration).

        Args:
            settings_source: Settings-like object to read the auto-scan flags
                from; defaults to the live settings singleton.

        Safe to call from any thread: lifecycle transitions are scheduled onto
        the event loop that owns the watch task.
        """
        cfg = settings_source if settings_source is not None else settings
        loop = self._loop
        if loop is None or loop.is_closed():
            # Never started on an event loop (e.g. constructed off-loop with
            # auto-scan disabled) — nothing to reconcile.
            return
        enabled = bool(getattr(cfg, "auto_scan_enabled", False))

        def _log_lifecycle_failure(fut: "concurrent.futures.Future") -> None:
            # The scheduling caller is a sync settings route on a worker
            # thread; it never inspects these futures. A silently-dropped
            # exception here leaves _running inconsistent with reality
            # (review F7, PR #576), so surface it in the logs.
            try:
                fut.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("FileWatcher lifecycle transition failed: %s", exc)

        if enabled:
            # The enabled path never reads lifecycle flags from the calling
            # thread (issue #693 / R9): the branch decision (pending-start
            # during a drain / start a stopped watcher / wake for a cadence
            # change) is owned by _ensure_running ON the watch loop, so a
            # back-to-back save racing a draining stop cannot silently drop
            # the restart.
            fut = asyncio.run_coroutine_threadsafe(self._ensure_running(), loop)
            fut.add_done_callback(_log_lifecycle_failure)
        elif self._running:
            fut = asyncio.run_coroutine_threadsafe(self.stop(), loop)
            fut.add_done_callback(_log_lifecycle_failure)

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
            conn = await pool.get_connection_async()
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
                # #650 review: _find_new_files does a blocking pooled
                # checkout (issue #645 flagged this helper explicitly) —
                # keep it off the event loop. The structural validation
                # screen (issue #693 / RT-S5-02) is the same class of
                # blocking IO (zip central-directory reads), so it runs in
                # the same thread context.
                new_files = await asyncio.to_thread(self._find_new_files, directory)
                for file_path in new_files:
                    ok, reason = await asyncio.to_thread(
                        _validate_scan_candidate, file_path
                    )
                    if not ok:
                        logger.warning(
                            "Scan rejected file failing structural validation "
                            "(%s); not enqueued: %s",
                            reason,
                            file_path,
                        )
                        continue
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

        # Get files from database. The stored ``file_path`` form depends on
        # how the row was written (absolute for route uploads, either form
        # for older rows) while the on-disk scan collects RESOLVED absolute
        # paths, so both sides are normalized (issue #693 / T1-27-KR-05):
        # the LIKE prefix matches BOTH spellings of the scan directory, and
        # membership compares resolved forms. Without this, a relative
        # ``data_dir`` makes one stored form miss the LIKE arm and the other
        # fail membership, re-enqueuing files that are already in the DB.
        files_in_db: Set[str] = set()
        try:
            if self.pool is None:
                from ..models.database import get_pool
                self.pool = get_pool(str(settings.sqlite_path), max_size=2)
            conn = self.pool.get_connection()
            try:
                cursor = conn.execute(
                    "SELECT file_path FROM files "
                    "WHERE file_path LIKE ? OR file_path LIKE ?",
                    (f"{str(directory)}%", f"{str(directory.resolve())}%"),
                )
                for row in cursor.fetchall():
                    files_in_db.add(str(Path(row["file_path"]).resolve()))
            finally:
                self.pool.release_connection(conn)
        except Exception as e:
            # Re-raise so scan_once treats a per-directory DB-query failure as
            # a counted/reported error (caught at the scan_once level, matching
            # the sibling scanning-error branch) rather than returning the same
            # empty set() as a legitimate "no new files" scan (RES-4).
            logger.error(f"Error querying database: {e}")
            raise

        # Find new files (on disk but not in DB) — resolved form vs resolved
        # form.
        new_files: Set[Path] = set()
        for file_path in files_on_disk:
            if str(file_path) not in files_in_db:
                new_files.add(file_path)

        return new_files

    async def _watch_loop(self) -> None:
        """
        Main watch loop that periodically scans directories.

        Continuously scans at configured intervals until shutdown_event is set.
        The interval is re-read from ``settings.auto_scan_interval_minutes`` on
        EVERY iteration (issue #494 CONFIG-001) instead of being captured once,
        and each wait also listens on the wake event so a reconciled cadence
        change applies promptly rather than after the old interval elapses.
        """
        while not self._shutdown_event.is_set():
            try:
                await self.scan_once()
            except Exception as e:
                logger.error(f"Error during scan: {e}")

            # Re-read the cadence every cycle so a saved interval change
            # applies without a watcher restart.
            interval_seconds = settings.auto_scan_interval_minutes * 60

            # Wait for the next scan interval, a reconcile wake, or shutdown —
            # whichever comes first.
            wake_wait = asyncio.ensure_future(self._wake_event.wait())
            shutdown_wait = asyncio.ensure_future(self._shutdown_event.wait())
            pending: Set[asyncio.Task] = {wake_wait, shutdown_wait}
            try:
                await asyncio.wait(
                    pending,
                    timeout=interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in pending:
                    task.cancel()
                # Consume the wake INSIDE the loop body, immediately after the
                # wait returns (review RP-001, PR #576): the event stays set
                # until explicitly cleared, so clearing it anywhere else (in
                # start(), or after the loop — which stop()'s task-cancel path
                # skips entirely) lets every subsequent wait resolve instantly
                # and turns the periodic scan into an unbounded busy loop.
                self._wake_event.clear()

        # Belt-and-suspenders for a clean loop exit (the cancel path above
        # already covers stop()-during-wait).
        self._wake_event.clear()

    @property
    def is_running(self) -> bool:
        return self._running
