"""Issue #727 acceptance checks AC7-AC8 (Workstream E, PR 1) — backup tag
lifecycle on the REAL lancedb store (temp dirs only, as
test_518_restore_drill_real.py does).

RED at base (HEAD 46e99488, no fix):
- AC7: tag names have one-second resolution (strftime "%Y%m%d%H%M%S"), so
  two create_backup_set runs of the same table within one wall-clock second
  collide and the second ``table.tags.create`` raises (lancedb "Ref conflict
  error: tag ... already exists").
- AC8: nothing ever deletes a ``backup-*`` tag. After the retention window
  the set is pruned but its tag stays, and every live
  ``table.optimize(cleanup_older_than=...)`` raises "Cleanup error: ...
  tagged version(s) ... marked for cleanup" — the error the app's periodic
  optimize() swallows as a warning, stopping compaction.

GREEN after the fix: same-second sets both complete (unique tag names), and
the documented pruner releases the pruned sets' backup-* tags so the live
table stays optimizable.
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lancedb  # noqa: E402
from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.cleanup_backups import cleanup_backups  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes

FROZEN_NOW = datetime(2027, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _key_provider():
    return (TEST_KEY, "9")


def _seed_app_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE markers(marker TEXT)")
        conn.execute("INSERT INTO markers VALUES ('backup_marker')")
        conn.commit()
    finally:
        conn.close()


def _make_table(lancedb_dir: Path):
    db = lancedb.connect(str(lancedb_dir))
    return db.create_table(
        "chunks",
        [{"chunk_id": i, "text": f"row-{i}"} for i in (1, 2)],
    )


class _FrozenDatetime(datetime):
    """datetime subclass whose now() always returns FROZEN_NOW, so two
    create_backup_set runs land in the same wall-clock second."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


def test_same_second_backups_both_complete(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _seed_app_db(data / "app.db")
    lancedb_dir = data / "lancedb"
    table = _make_table(lancedb_dir)

    monkeypatch.setattr(backup_set_module.settings, "data_dir", data)
    monkeypatch.setattr(backup_set_module, "datetime", _FrozenDatetime)

    create_backup_set(
        tmp_path / "set-1",
        lancedb_dir=lancedb_dir,
        lancedb_tables={"chunks": table},
        key_provider=_key_provider,
    )

    second_ok = True
    try:
        create_backup_set(
            tmp_path / "set-2",
            lancedb_dir=lancedb_dir,
            lancedb_tables={"chunks": table},
            key_provider=_key_provider,
        )
    except Exception:
        second_ok = False

    assert second_ok is True, (
        "E-01-AC7 SAME-SECOND TAG COLLISION: the second backup set of the "
        "same live table within one wall-clock second raised — tag names "
        "have one-second resolution so both runs derive the same "
        "backup-<timestamp> tag and the second table.tags.create hits the "
        f"existing tag (live tags: {sorted(table.tags.list())})"
    )


def test_pruned_sets_release_their_tags(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _seed_app_db(data / "app.db")
    lancedb_dir = data / "lancedb"
    table = _make_table(lancedb_dir)
    backups_dir = data / "backups"
    set_dir = backups_dir / "set-1"

    monkeypatch.setattr(backup_set_module.settings, "data_dir", data)
    create_backup_set(
        set_dir,
        lancedb_dir=lancedb_dir,
        lancedb_tables={"chunks": table},
        key_provider=_key_provider,
    )

    # Writes past the tagged generation, so newer versions exist on the live
    # table when the retention window closes.
    table.add(
        [{"chunk_id": i, "text": "post-backup"} for i in (101, 102, 103)]
    )

    # Age the set directory (and its contents) past retention.
    aged = time.time() - 40 * 24 * 3600
    for path in [set_dir, *set_dir.rglob("*")]:
        os.utime(path, (aged, aged))

    cleanup_backups(backups_dir, retention_days=0)

    backup_tags = [
        name for name in table.tags.list() if name.startswith("backup-")
    ]
    assert len(backup_tags) == 0, (
        "E-01-AC8 TAGS NEVER RELEASED: the documented pruner removed the "
        "expired set but its live backup-* tag survives ("
        f"{backup_tags}); nothing deletes backup tags, so every later "
        "table.optimize() raises a cleanup error the app swallows as a "
        "warning"
    )

    # Follow-on control: with the tag released, the live table's periodic
    # optimize must run clean (at base this raises "Cleanup error: tagged
    # version(s) marked for cleanup").
    table.optimize(cleanup_older_than=timedelta(0))
