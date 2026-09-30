"""B04 acceptance checks (issue #693) — FileWatcher new-file detection + lifecycle.

Frozen spec for the fix (RED at base dc894f49). Three contracts, all against
a REAL ``SQLiteConnectionPool`` (never a mocked pool) so the ``file_path``
comparison the watcher actually performs is what fails:

- ``test_relative_stored_row_not_reported_new`` (AC2) and
  ``test_absolute_stored_row_not_reported_new`` (AC3) — a file whose ``files``
  row already exists must never be reported as new, regardless of whether the
  stored ``file_path`` is the relative or the resolved absolute form. At the
  base ``FileWatcher._find_new_files`` compares resolved absolute on-disk
  paths against the RAW stored strings under a ``file_path LIKE '<dir>%'``
  prefix filter, so under a relative ``data_dir`` BOTH forms slip through and
  the file is re-enqueued on every scan. RED evidence: ``assert 1 == 0``.

- ``test_reconcile_enable_during_stop_drain_leaves_watcher_running`` (AC4) —
  ``reconcile(enabled=True)`` arriving while ``stop()`` is still draining the
  in-flight scan must leave the watcher RUNNING. At the base reconcile only
  sets the wake event (it sees ``_running`` still True) and the drain then
  completes the stop, so the watcher ends stopped. RED evidence:
  ``assert 0 == 1``.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch


def _seed_vault(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        conn.commit()
        return int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    finally:
        conn.close()


def _seed_files_row(db_path: str, vid: int, stored_path: str, name: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (vid, stored_path, name, 1, "indexed"),
        )
        conn.commit()
    finally:
        conn.close()


async def test_relative_stored_row_not_reported_new(tmp_path: Path) -> None:
    """AC2: a relative-form stored file_path must not count as a new file.

    The data_dir is relative (``data``) under a chdir'd tmp cwd — the exact
    production shape where the watcher's LIKE-prefix fetch DOES return the
    stored relative row but the set membership check then compares it against
    a resolved absolute on-disk path and misses.
    """
    from app.config import settings
    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.file_watcher import FileWatcher

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        db_path = str(Path("data") / "app.db")
        with patch.object(settings, "data_dir", Path("data")):
            init_db(db_path)
            vid = _seed_vault(db_path)
            uploads = settings.vault_uploads_dir(vid)  # relative form
            (uploads / "rel.txt").write_text("x", encoding="utf-8")
            _seed_files_row(db_path, vid, str(uploads / "rel.txt"), "rel.txt")

            pool = SQLiteConnectionPool(db_path, max_size=2)
            try:
                fw = FileWatcher(processor=MagicMock(), pool=pool)
                new = fw._find_new_files(uploads)
            finally:
                pool.close_all()
    finally:
        os.chdir(old_cwd)

    assert len(new) == 0


async def test_absolute_stored_row_not_reported_new(tmp_path: Path) -> None:
    """AC3: an absolute-form stored file_path must not count as a new file.

    Mirror of the AC2 harness with the resolved absolute form stored: the
    LIKE '<relative-dir>%' prefix filter excludes the absolute row entirely,
    so the on-disk file is reported new.
    """
    from app.config import settings
    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.file_watcher import FileWatcher

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        db_path = str(Path("data") / "app.db")
        with patch.object(settings, "data_dir", Path("data")):
            init_db(db_path)
            vid = _seed_vault(db_path)
            uploads = settings.vault_uploads_dir(vid)  # relative form
            target = uploads / "abs.txt"
            target.write_text("x", encoding="utf-8")
            _seed_files_row(db_path, vid, str(target.resolve()), "abs.txt")

            pool = SQLiteConnectionPool(db_path, max_size=2)
            try:
                fw = FileWatcher(processor=MagicMock(), pool=pool)
                new = fw._find_new_files(uploads)
            finally:
                pool.close_all()
    finally:
        os.chdir(old_cwd)

    assert len(new) == 0


async def test_reconcile_enable_during_stop_drain_leaves_watcher_running() -> None:
    """AC4: reconcile(enabled=True) during stop()'s drain must not end stopped.

    The watcher starts with a slow (0.3s) in-flight scan; stop() begins
    draining it; the user then re-enables auto-scan (reconcile fires with
    ``auto_scan_enabled`` True). The end state must be a RUNNING watcher —
    the later intent wins. ``int()`` capture keeps the frozen assert free of
    an E712 ``== True`` comparison.
    """
    from app.config import settings
    from app.services.file_watcher import FileWatcher

    async def _slow_scan() -> None:
        await asyncio.sleep(0.3)

    with patch.object(settings, "auto_scan_enabled", True):
        fw = FileWatcher(processor=MagicMock(), pool=None)
        fw.scan_once = _slow_scan
        await fw.start()
        t = asyncio.create_task(fw.stop())
        await asyncio.sleep(0)  # let stop() begin draining the in-flight scan
        fw.reconcile(settings)
        await t
        await asyncio.sleep(0.5)
        running = int(fw.is_running)

    assert running == 1
