"""Issue #728 acceptance check AC5 (Workstream E, PR 2) — a restore that
completes through the operator CLI's LanceDB factory must leave each
restored table so a FRESH connection opens the manifest-tagged version.
Uses real lancedb in a temp dir.

RED at base (HEAD 46e99488, no fix): ``table.checkout(tag)`` runs on a
handle the CLI factory opened into a closure dict that dies with the
process/handle; a fresh ``lancedb.connect(<dest>/lancedb).open_table(...)``
still serves the tree's LATEST version. Audit probe_a saw version 2 where
the manifest tagged version 1.

Setup: the table handed to create_backup_set is wrapped in a proxy whose
``tags.create`` delegates and then ``add()``s rows, so the copied tree's
latest version is NEWER than the tagged one — exactly the live-store shape
(backups taken while writes continue) that makes an unpersisted checkout
observable.

GREEN after the fix: a fresh connection to <dest>/lancedb opens the table
at the tagged generation (10 rows, not 15).
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lancedb  # noqa: E402
from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402
from scripts import restore as restore_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes

TAGGED_ROWS = 10
POST_TAG_ROWS = 5
TABLE_NAME = "chunks"


def _key_provider():
    return (TEST_KEY, "9")


def _seed_source_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE markers(marker TEXT)")
        conn.execute("INSERT INTO markers VALUES ('backup_marker')")
        conn.commit()
    finally:
        conn.close()


class _PostTagWriteTags:
    """Delegates tags.create, then adds rows to the real table so the
    copied tree's latest version is newer than the tag."""

    def __init__(self, real_table, extra_rows):
        self._real_table = real_table
        self._extra_rows = extra_rows

    def create(self, tag, version):
        result = self._real_table.tags.create(tag, version)
        self._real_table.add(self._extra_rows)
        return result


class _PostTagWriteTableProxy:
    """Duck-typed table for create_backup_set: exposes no ``create_tag`` (so
    the real-API ``table.tags.create`` path runs) plus the ``version`` the
    caller reads next to it."""

    def __init__(self, real_table, extra_rows):
        self._real_table = real_table
        self.tags = _PostTagWriteTags(real_table, extra_rows)

    @property
    def version(self):
        return self._real_table.version


def test_tagged_generation_persists_for_fresh_connection(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _seed_source_db(src / "app.db")

    lancedb_dir = tmp_path / "lancedb"
    real_table = lancedb.connect(str(lancedb_dir)).create_table(
        TABLE_NAME,
        [
            {"chunk_id": i, "text": f"row-{i}"}
            for i in range(1, TAGGED_ROWS + 1)
        ],
    )
    proxy = _PostTagWriteTableProxy(
        real_table,
        [
            {"chunk_id": i, "text": "post-tag"}
            for i in range(101, 101 + POST_TAG_ROWS)
        ],
    )

    monkeypatch.setattr(
        backup_set_module,
        "settings",
        SimpleNamespace(sqlite_path=str(src / "app.db")),
    )
    out = tmp_path / "set"
    create_backup_set(
        out,
        lancedb_dir=lancedb_dir,
        lancedb_tables={TABLE_NAME: proxy},
        key_provider=_key_provider,
    )

    dest = tmp_path / "dest"
    restore_backup_set(
        out,
        dest,
        lancedb_factory=restore_module._cli_lancedb_factory(dest),
        key_provider=_key_provider,
    )

    fresh = lancedb.connect(str(dest / "lancedb")).open_table(TABLE_NAME)
    assert fresh.count_rows() == TAGGED_ROWS, (
        "E-02-AC5 CHECKOUT NOT PERSISTED: a fresh connection to "
        f"<dest>/lancedb serves {fresh.count_rows()} rows, not the "
        f"{TAGGED_ROWS}-row tagged generation — the CLI factory's "
        "checkout(tag) ran on a discarded handle, so the restored table's "
        "latest pointer still serves post-tag writes"
    )
