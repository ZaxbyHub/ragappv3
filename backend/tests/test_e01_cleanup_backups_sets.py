"""Issue #727 acceptance check AC9 (Workstream E, PR 1) — the documented
retention pruner must remove expired backup SET directories, not only the
legacy single-file ``db_*_k*.enc`` names.

RED at base (HEAD 46e99488, no fix): ``cleanup_backups`` globs
``db_*_k*.enc`` only, so a directory holding a ``backup_manifest.json``
(what scripts/backup_set.py produces and docs/operations.md schedules) is
never matched and outlives every retention window.

GREEN after the fix: set directories older than the retention period are
removed; newer ones survive.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.cleanup_backups import cleanup_backups  # noqa: E402

DAY = 24 * 3600


def _make_set(root: Path, name: str, age_days: float) -> None:
    set_dir = root / name
    set_dir.mkdir()
    manifest = set_dir / "backup_manifest.json"
    manifest.write_text('{"schema": "ragapp-backup-set/1", "items": []}')
    aged = time.time() - age_days * DAY
    os.utime(manifest, (aged, aged))


def test_pruner_removes_expired_set_directories(tmp_path):
    _make_set(tmp_path, "set-old", age_days=40)  # past the 30-day window
    _make_set(tmp_path, "set-new", age_days=1)  # well inside it

    cleanup_backups(tmp_path, retention_days=30)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["set-new"], (
        "E-01-AC9 PRUNER NEVER MATCHES SETS: after "
        "cleanup_backups(<dir>, retention_days=30) the backup-set "
        "directories are "
        f"{sorted(p.name for p in tmp_path.iterdir())} — the documented "
        "pruner globs only legacy db_*_k*.enc files, so the expired "
        "set-old/ survives its retention window"
    )
