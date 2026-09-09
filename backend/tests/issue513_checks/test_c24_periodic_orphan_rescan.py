"""C24 - AC24 (FU-007): a PERIODIC orphan rescan must exist, recover expired
orphans exactly once, and ignore live leases.

Contract under test (issue #513 AC24):

  Part A (capability, discriminating) - beyond the two known hourly sweeps
  (vector-delete + artifact-delete) and the workers, the background processor
  must own a PERIODIC stranded-row/orphan rescan mechanism, surfaced as either
  a new periodically-scheduled asyncio task attribute on the processor (e.g.
  an orphan/stranded/rescan sweep task) or a settings interval knob named for
  it (orphan/rescan/stranded interval). Startup-only sweeps do not satisfy the
  contract: an orphan that appears AFTER startup must be recoverable without a
  restart.

  Part B (behavior) - with the processor running and workers consuming:
    * an EXPIRED orphan (status='processing', phase_started_at older than the
      stranded timeout, file present) is recovered (completes) by the rescan,
      and two rescan ticks do not duplicate its publication (the file is
      processed exactly once);
    * a LIVE lease (status='processing', young phase_started_at - an actively
      parsing job) is ignored: never enqueued, never stolen.

Pre-fix expectation: only startup recovery exists (no periodic orphan rescan
attribute/knob) -> Part A FAIL (headline). Part B then still guides the
post-fix behavior (it passes pre-fix but is meaningless without Part A).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c24_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

_WAIT_S = 8.0

_KNOWN_TASK_ATTRS = {
    "_worker_tasks",
    "_enrichment_worker_task",
    "_atom_enrichment_worker_task",
    "_reindex_worker_task",
    "_vector_delete_sweep_task",
    "_artifact_delete_sweep_task",
}
_KNOB_RE = re.compile(r"orphan|rescan|stranded", re.IGNORECASE)


def _periodic_capability(processor, settings) -> tuple[bool, str]:  # noqa: ANN001
    """True when a periodic orphan/stranded rescan capability is present."""
    new_tasks = []
    for name, value in vars(processor).items():
        if name in _KNOWN_TASK_ATTRS:
            continue
        if isinstance(value, asyncio.Task):
            new_tasks.append(name)
        elif isinstance(value, list) and value and all(isinstance(t, asyncio.Task) for t in value):
            new_tasks.append(name)
    if new_tasks:
        return True, f"periodic task attribute(s): {', '.join(sorted(new_tasks))}"
    knobs = [name for name in dir(settings) if _KNOB_RE.search(name)]
    if knobs:
        return True, f"settings knob(s): {', '.join(sorted(knobs))}"
    return False, ""


def _file_status(db_path: str, file_id: int) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status FROM files WHERE id = ?", (file_id,)).fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


async def _scenario_part_b(db_path: str, tmp: Path) -> str:
    """Returns a FAIL reason ('' = pass). Requires a running processor."""
    import app.services.background_tasks as bt
    from app.models.database import get_pool

    conn = sqlite3.connect(db_path)
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    orphan_path = tmp / "orphan.txt"
    orphan_path.write_text("orphan content", encoding="utf-8")
    live_path = tmp / "live.txt"
    live_path.write_text("live content", encoding="utf-8")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status, "
        "phase, phase_started_at) VALUES (?, ?, ?, ?, ?, 'processing', 'embedding', "
        "datetime('now', '-120 minutes'))",
        (vault_id, str(orphan_path), "orphan.txt", "hc24a", 8),
    )
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status, "
        "phase, phase_started_at) VALUES (?, ?, ?, ?, ?, 'processing', 'parsing', "
        "datetime('now', '-5 seconds'))",
        (vault_id, str(live_path), "live.txt", "hc24b", 8),
    )
    conn.commit()
    orphan_id = conn.execute("SELECT id FROM files WHERE file_hash = 'hc24a'").fetchone()[0]
    live_id = conn.execute("SELECT id FROM files WHERE file_hash = 'hc24b'").fetchone()[0]
    conn.close()

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    processor = bt.BackgroundProcessor(
        max_retries=1, retry_delay=0.01, pool=get_pool(db_path, max_size=3)
    )
    processed: list[int] = []
    enqueued: list[int] = []

    async def fake_process_existing_file(file_id, file_path, vault_id):  # noqa: ANN202
        processed.append(file_id)
        with contextlib.closing(sqlite3.connect(db_path)) as c:
            c.execute("UPDATE files SET status='indexed' WHERE id = ?", (file_id,))
            c.commit()
        return None

    processor.processor.process_existing_file = fake_process_existing_file
    orig_enqueue = processor.enqueue

    async def counting_enqueue(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        fid = kwargs.get("file_id")
        if fid is not None:
            enqueued.append(int(fid))
        await orig_enqueue(*args, **kwargs)

    processor.enqueue = counting_enqueue
    try:
        await asyncio.wait_for(processor.start(), timeout=6.0)
        # Give any startup sweep a moment; orphans were seeded AFTER start()'s
        # sweep in this scenario only if start() ran before seeding - here they
        # predate start(), so the startup sweep may legitimately recover the
        # expired one. The periodic ticks below must not then re-publish it.
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline and _file_status(db_path, orphan_id) != "indexed":
            await asyncio.sleep(0.1)

        # Two periodic rescan ticks back to back.
        rescan = getattr(processor, "_recover_stranded_pending_rows", None)
        if rescan is not None:
            await rescan()
            await rescan()
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline and _file_status(db_path, orphan_id) != "indexed":
            await asyncio.sleep(0.1)

        orphan_status = _file_status(db_path, orphan_id)
        live_status = _file_status(db_path, live_id)
        orphan_runs = processed.count(orphan_id)
        live_runs = processed.count(live_id)

        if orphan_status != "indexed" or orphan_runs < 1:
            return (
                f"expired orphan (file_id={orphan_id}) not recovered by the rescan: "
                f"status='{orphan_status}', processed {orphan_runs} time(s) (FU-007)"
            )
        if orphan_runs > 1:
            return (
                f"duplicate publication: expired orphan (file_id={orphan_id}) processed "
                f"{orphan_runs} times across rescan ticks (FU-007)"
            )
        if live_status != "processing" or live_runs > 0:
            return (
                f"live lease stolen: actively-processing row (file_id={live_id}) "
                f"status='{live_status}', processed {live_runs} time(s) - a long "
                f"legitimate parse must not be recovered by age alone (FU-007)"
            )
        return ""
    finally:
        processor.shutdown_event.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(processor.stop(timeout=1.0), timeout=5.0)
        processor._running = False
        bt._processor_instance = orig_instance


async def _scenario() -> str:
    import app.services.background_tasks as bt
    from app.config import settings
    from app.models.database import get_pool, init_db

    tmp = Path(tempfile.mkdtemp(prefix="c24_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    conn.commit()
    conn.close()

    # Part A: capability probe against a fully started processor.
    orig_instance = bt._processor_instance
    bt._processor_instance = None
    cap_found, cap_detail = False, ""
    processor = bt.BackgroundProcessor(
        max_retries=1, retry_delay=0.01, pool=get_pool(db_path, max_size=3)
    )
    try:
        await asyncio.wait_for(processor.start(), timeout=6.0)
        cap_found, cap_detail = _periodic_capability(processor, settings)
    finally:
        processor.shutdown_event.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(processor.stop(timeout=1.0), timeout=5.0)
        processor._running = False
        bt._processor_instance = orig_instance

    if not cap_found:
        return (
            "no periodic orphan rescan exists - recovery is startup-only "
            "(no periodic task attribute beyond the known workers/vector-delete/"
            "artifact-delete sweeps, and no orphan/rescan/stranded settings knob), "
            "so an orphan appearing after startup is stranded until a restart (FU-007)"
        )
    _ = cap_detail

    # Part B: behavioral verification of the rescan.
    return await _scenario_part_b(db_path, tmp)


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C24 CHECK: FAIL: {reason}")
        return 1
    print("C24 CHECK: PASS")
    return 0


def test_c24_periodic_orphan_rescan() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
