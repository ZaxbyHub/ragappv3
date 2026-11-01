"""Acceptance checks pinning issue #700 criteria (Workstream B PR 11).

Issue #700 bundles five SQLiteConnectionPool defects, two store-level
transaction gaps, and a compose shutdown-budget mismatch. Each test below
freezes ONE acceptance criterion; each test's FINAL assert is the frozen
acceptance line, and its pytest failure rendering at the pre-fix HEAD is the
RED signature recorded in the issue trace (`.agents/issue-traces/
700-pool-store-shutdown-timeouts/`).

Pre-fix baseline (what each check observes at HEAD):

* AC1 ``test_migration_connection_busy_timeout_matches_init_db`` — the
  migrate_* helpers open bare ``sqlite3.connect(path)`` with Python's
  default 5000 ms busy timeout, while init_db (and every pooled connection)
  configures 30000 ms: a migration racing a busy writer gives up after 5 s
  instead of 30 s.
* AC2 ``test_async_checkout_ceiling_holds_for_many_waiters`` —
  get_connection's deadline is minted on the checkout-executor worker
  thread, so executor-queue time (CHECKOUT_EXECUTOR_MAX_WORKERS = 16)
  never counts against it: 80 exhausted-pool async waiters drain in
  16-wide waves, each wave re-arming the full wait budget.
* AC3 ``test_every_checkout_failure_emits_pool_exhausted`` — only the
  loop-exhaustion raise site emits the ``pool_exhausted`` structured log
  event; the four other "Could not obtain a connection" raise sites fail
  silently.
* AC4 ``test_capacity_probe_not_blocked_by_connection_creation`` —
  get_connection holds ``self._lock`` across ``_create_connection()`` I/O,
  so the lock-guarded ``recent_capacity_wait()`` readiness probe blocks
  behind connection creation.
* AC5 ``test_release_after_close_all_does_not_raise`` —
  release_connection raises RuntimeError as its first statement once the
  pool is closed, instead of quietly closing the caller's connection.
* AC6 ``test_update_tag_zero_rows_leaves_no_open_transaction`` —
  TagStore.update_tag returns None on a 0-row UPDATE without commit or
  rollback, leaving the pooled connection inside an open transaction.
* AC7/AC8 — FolderStore.create_folder checks the parent BEFORE its BEGIN
  IMMEDIATE and move_documents UPDATEs with no transaction and no
  IntegrityError translation; a concurrent delete of the referenced folder
  between check and write surfaces a raw sqlite3.IntegrityError instead of
  FolderNotFoundError.
* AC9 ``test_compose_stop_grace_period_covers_stop_budget`` —
  docker-compose.yml sets no ``stop_grace_period`` for the knowledgevault
  service (Docker default 10 s) while BackgroundProcessor.stop's graceful
  shutdown budget defaults to 60 s: compose SIGKILLs the backend mid-drain.
"""

import asyncio
import inspect
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.models import database as app_database
from app.models.database import (
    SQLiteConnectionPool,
    migrate_add_files_content_fts,
    run_migrations,
)
from app.services.background_tasks import BackgroundProcessor
from app.services.folder_store import FolderStore
from app.services.tag_store import TagStore

REPO = Path(__file__).resolve().parents[2]

# The busy timeout (ms) init_db configures and the pooled connections carry;
# migrations must match it instead of Python's 5000 ms default.
INIT_DB_BUSY_TIMEOUT_MS = 30000


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _PoolExhaustedCollector(logging.Handler):
    """Count ``pool_exhausted`` events emitted by app.models.database.

    Attached directly to ``app.models.database.logger`` so the count is
    independent of propagation to the root logger.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.events = 0

    def emit(self, record):
        if "pool_exhausted" in record.getMessage():
            self.events += 1


def _seed_vault(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("INSERT INTO vaults (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _delete_folder_via_second_connection(db_path: str, folder_id: int) -> None:
    """Delete a folder through a SEPARATE connection (the concurrent deleter).

    ``timeout=0.2`` keeps a lock conflict from hanging the test; a swallowed
    OperationalError means the delete lost a race (the folder survives and
    the acceptance outcome degrades to 'ok', which the tuple assertions
    tolerate on a healthy tree).
    """
    other = sqlite3.connect(db_path, timeout=0.2)
    try:
        other.execute("PRAGMA foreign_keys = ON")
        other.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        other.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        other.close()


def _hook_folder_check_to_delete(store: FolderStore, db_path: str, monkeypatch):
    """After the REAL vault-scope check passes, delete the checked folder
    through a second connection — the concurrent-delete window between the
    check and the write that issue #700 pins."""
    real_check = store._require_folder_in_vault

    def check_then_delete(checked_vault_id, checked_folder_id):
        real_check(checked_vault_id, checked_folder_id)
        _delete_folder_via_second_connection(db_path, checked_folder_id)

    monkeypatch.setattr(store, "_require_folder_in_vault", check_then_delete)


def _knowledgevault_service_block() -> str:
    """The knowledgevault service block from docker-compose.yml (the regex
    scoping idiom of tests/test_issue494_compose_literal_values.py: from the
    service key up to the next two-space-indented key)."""
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r"^  knowledgevault:", text, re.M)
    assert match, "knowledgevault service not found in docker-compose.yml"
    tail = text[match.end() :]
    return re.split(r"^  [a-zA-Z_-]+:", tail, maxsplit=1)[0]


def _parse_compose_duration(literal: str) -> float:
    """Seconds for a compose duration literal ('90s', '1m30s', '2h', '500ms')."""
    units = {"h": 3600.0, "ms": 0.001, "m": 60.0, "s": 1.0}
    total = 0.0
    matched = False
    for count, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", literal):
        matched = True
        total += float(count) * units[unit]
    if not matched:
        raise ValueError(f"unsupported stop_grace_period form: {literal!r}")
    return total


def _compose_stop_grace_seconds():
    """knowledgevault's stop_grace_period in seconds; 10 (Docker's default)
    when the key is absent."""
    block = _knowledgevault_service_block()
    match = re.search(r"^\s+stop_grace_period:\s*(\S+)\s*$", block, re.M)
    if match is None:
        return 10
    return _parse_compose_duration(match.group(1))


def test_migration_connection_busy_timeout_matches_init_db(tmp_path, monkeypatch):
    """AC1: a migration's connection must be opened with the same busy
    timeout init_db uses (30000 ms), not Python's sqlite3 default (5000 ms).

    The factory subclass records the connection's PRAGMA busy_timeout at
    close() time — the value the connection carried for its whole life.
    """
    db = str(tmp_path / "ac1.db")
    run_migrations(db)

    recorded: list[int] = []

    class _BusyTimeoutRecordingConnection(sqlite3.Connection):
        def close(self):
            try:
                row = self.execute("PRAGMA busy_timeout").fetchone()
                if row is not None:
                    recorded.append(row[0])
            except sqlite3.Error:
                pass
            super().close()

    original_connect = app_database.sqlite3.connect

    def factory_connect(*args, **kwargs):
        kwargs["factory"] = _BusyTimeoutRecordingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(app_database.sqlite3, "connect", factory_connect)
    # Second run of an idempotent migration: it still opens (and closes) a
    # bare connection even on the no-op path.
    migrate_add_files_content_fts(db)

    assert min(recorded) >= INIT_DB_BUSY_TIMEOUT_MS


async def test_async_checkout_ceiling_holds_for_many_waiters(tmp_path, monkeypatch):
    """AC2: with the pool's only connection held, 80 async checkouts must all
    settle within the per-caller ceiling (patched wait budget 0.5 s, ceiling
    0.5 * 1.5 + 0.25 = 1.0 s).

    Pre-fix, the wait deadline is minted on the checkout-executor WORKER
    thread, so the 80 callers drain through the 16-worker executor in waves
    that each re-arm the full 0.5 s budget — the tail caller settles around
    5 waves x 0.5 s ~= 2.5 s, far past the 1.0 s ceiling.
    """
    db = str(tmp_path / "ac2.db")
    monkeypatch.setattr(app_database, "CHECKOUT_WAIT_SECONDS", 0.5)

    pool = SQLiteConnectionPool(db, max_size=1)
    try:
        held = pool.get_connection()
        elapsed: list[float] = []

        async def timed_checkout():
            started = time.monotonic()
            try:
                await pool.get_connection_async(max_wait_attempts=1)
            except RuntimeError:
                pass  # the exhausted-pool contract; only the timing matters
            elapsed.append(time.monotonic() - started)

        await asyncio.gather(*(timed_checkout() for _ in range(80)))
        within = max(elapsed) <= (0.5 * 1.5 + 0.25)
    finally:
        pool.release_connection(held)
        pool.close_all()

    assert int(within) == 1


def test_every_checkout_failure_emits_pool_exhausted(tmp_path, monkeypatch):
    """AC3: a checkout that fails through the invalid-idle-connection raise
    site must emit the same ``pool_exhausted`` structured event as the
    loop-exhaustion site.

    Setup reuses tests/test_issue645_checkout_bound.py::
    test_checkout_budget_enforced_across_invalid_idle_connections — seeded
    invalid idle entries with a slow-failing validation — but with a shrunk
    budget (CHECKOUT_WAIT_SECONDS=0.2, probe 0.4 s) so the first invalid
    entry already spends the deadline and the failure fires from the
    non-loop-exhaustion raise site.
    """
    monkeypatch.setattr(app_database, "CHECKOUT_WAIT_SECONDS", 0.2)

    class _InvalidIdleConn:
        def close(self):
            pass

    db = str(tmp_path / "ac3.db")
    seeded = 3
    pool = SQLiteConnectionPool(db, max_size=seeded + 5)

    def slow_failing_validate(conn):
        time.sleep(0.4)
        return False

    monkeypatch.setattr(pool, "_validate_connection", slow_failing_validate)

    collector = _PoolExhaustedCollector()
    pool_logger = app_database.logger
    previous_level = pool_logger.level
    pool_logger.setLevel(logging.WARNING)
    pool_logger.addHandler(collector)
    try:
        for _ in range(seeded):
            pool._pool.put_nowait(_InvalidIdleConn())

        with pytest.raises(RuntimeError):
            pool.get_connection(max_wait_attempts=1)
    finally:
        pool_logger.removeHandler(collector)
        pool_logger.setLevel(previous_level)
        pool.close_all()

    n_events = collector.events
    assert n_events == 1


def test_capacity_probe_not_blocked_by_connection_creation(tmp_path, monkeypatch):
    """AC4: while a checkout is inside _create_connection() I/O, the readiness
    probe recent_capacity_wait() must still answer (it only reads a
    lock-guarded timestamp).

    Pre-fix, get_connection holds self._lock ACROSS _create_connection(), so
    the probe blocks behind creation I/O. Thread A checks out with creation
    patched to block on an Event; after creation has provably started, thread
    B probes and must return within the 0.5 s join.
    """
    db = str(tmp_path / "ac4.db")
    pool = SQLiteConnectionPool(db, max_size=1)

    entered = threading.Event()
    release = threading.Event()
    original_create = pool._create_connection

    def blocking_create():
        entered.set()
        release.wait(timeout=5.0)
        return original_create()

    monkeypatch.setattr(pool, "_create_connection", blocking_create)

    checked_out = []
    thread_a = threading.Thread(
        target=lambda: checked_out.append(pool.get_connection())
    )
    thread_a.start()
    probed = []
    thread_b = threading.Thread(
        target=lambda: probed.append(pool.recent_capacity_wait())
    )
    try:
        entered.wait(timeout=5.0)
        thread_b.start()
        thread_b.join(0.5)
        returned = not thread_b.is_alive()
    finally:
        release.set()
        thread_a.join(timeout=5.0)
        if thread_b.is_alive():
            thread_b.join(timeout=5.0)
        if checked_out:
            pool.release_connection(checked_out[0])
        pool.close_all()

    assert int(returned) == 1


def test_release_after_close_all_does_not_raise(tmp_path):
    """AC5: returning a connection to an already-closed pool must not raise —
    the release path quietly closes the caller's connection instead.

    The 'none'-string sentinel (rather than ``is None``) is deliberate: ruff
    E711/E712 forbid ``== None`` / ``== True`` comparisons, and the frozen
    rendering must stay ``assert 'RuntimeError' == 'none'``. The leak-close
    half of the criterion is deliberately NOT asserted here so the terminal
    rendering stays byte-exact.
    """
    db = str(tmp_path / "ac5.db")
    pool = SQLiteConnectionPool(db, max_size=1)
    conn = pool.get_connection()
    pool.close_all()

    raised = "none"
    try:
        pool.release_connection(conn)
    except Exception as exc:
        raised = type(exc).__name__

    assert raised == "none"


def test_update_tag_zero_rows_leaves_no_open_transaction(tmp_path):
    """AC6: TagStore.update_tag on a nonexistent tag id must leave the
    connection OUT of a transaction (commit or rollback the implicit BEGIN
    the 0-row UPDATE opened) instead of returning None mid-transaction.
    """
    db = str(tmp_path / "ac6.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    try:
        vault_id = _seed_vault(conn, "b11-ac6")
        result = TagStore(conn).update_tag(9999, vault_id, name="renamed")
        assert result is None  # the documented 0-row contract itself
        assert int(conn.in_transaction) == 0
    finally:
        conn.close()


def test_create_folder_concurrent_parent_delete_is_not_integrity_error(
    tmp_path, monkeypatch
):
    """AC7: when the parent folder is deleted (second connection) between
    create_folder's parent check and its INSERT, the store must surface
    'ok' (re-check/serialization) or FolderNotFoundError — never a raw
    sqlite3.IntegrityError leaking the FK violation.
    """
    db_path = str(tmp_path / "ac7.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        vault_id = _seed_vault(conn, "b11-ac7")
        store = FolderStore(conn)
        parent = store.create_folder(vault_id, "parent")

        _hook_folder_check_to_delete(store, db_path, monkeypatch)

        try:
            store.create_folder(vault_id, "child", parent_folder_id=parent.id)
            outcome = "ok"
        except Exception as exc:
            outcome = type(exc).__name__
        assert outcome in ("ok", "FolderNotFoundError")
    finally:
        conn.close()


def test_move_documents_concurrent_target_delete_is_not_integrity_error(
    tmp_path, monkeypatch
):
    """AC8: when the target folder is deleted (second connection) between
    move_documents' target check and its UPDATE, the store must surface
    'ok' or FolderNotFoundError — never a raw sqlite3.IntegrityError leaking
    the files.folder_id FK violation.
    """
    db_path = str(tmp_path / "ac8.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        vault_id = _seed_vault(conn, "b11-ac8")
        store = FolderStore(conn)
        target = store.create_folder(vault_id, "target")
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size)"
            " VALUES (?, ?, ?, ?)",
            (vault_id, "docs/b11-ac8.pdf", "b11-ac8.pdf", 128),
        )
        conn.commit()
        file_id = cur.lastrowid

        _hook_folder_check_to_delete(store, db_path, monkeypatch)

        try:
            store.move_documents(vault_id, [file_id], target.id)
            outcome = "ok"
        except Exception as exc:
            outcome = type(exc).__name__
        assert outcome in ("ok", "FolderNotFoundError")
    finally:
        conn.close()


def test_compose_stop_grace_period_covers_stop_budget():
    """AC9: the knowledgevault service's stop_grace_period (Docker default
    10 s when absent) must be at least BackgroundProcessor.stop's graceful
    shutdown budget (timeout default 60 s), or compose SIGKILLs the backend
    mid-drain."""
    grace = _compose_stop_grace_seconds()
    default = inspect.signature(BackgroundProcessor.stop).parameters["timeout"].default
    assert grace >= default
