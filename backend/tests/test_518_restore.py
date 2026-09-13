"""Issue #518 acceptance check C5b / AC5 (restore half) — full backup set
with manifest binding + LanceDB version tags + restore drill.

NEW-SURFACE spec (frozen). Two new scripts must exist:

``scripts/backup_set.py`` with::

    def create_backup_set(
        output_dir: Path,
        *,
        lancedb_dir: Path | None = None,
        lancedb_tables: Mapping[str, Any] | None = None,
        vault_dirs: Sequence[Path] = (),
        draft_room_dir: Path | None = None,
        key_provider: Callable[[], tuple[bytes, str]] | None = None,
    ) -> Path

- SQLite source: ``settings.sqlite_path`` (WAL-safe copy). Artifact
  ``app.db.enc`` = 12-byte nonce || AES-GCM ciphertext; key from
  ``key_provider`` (default: module-level SecretManager name, mirroring
  scripts/backup_sqlite.py).
- LanceDB: copies the whole ``lancedb_dir`` tree into the backup set AND,
  BEFORE copying, pins each provided table's version by calling
  ``table.create_tag(tag)`` with a tag beginning ``"backup-"``.
- Vault dirs and the draft-room dir are copied into the set.
- Writes ``output_dir/backup_manifest.json`` — JSON::

    {"schema": "ragapp-backup-set/1", "items": [
       {"name": "app.db", "kind": "sqlite", "path": "app.db.enc",
        "sha256": "<hex of artifact>"},
       {"name": "lancedb", "kind": "lancedb", "path": "lancedb",
        "tables": [{"table": "<name>", "tag": "backup-..."}],
        "files": {"<relpath>": "<sha256>"}},
       {"name": "<dir name>", "kind": "vault", "path": "vaults/<name>",
        "files": {"<relpath>": "<sha256>"}},
       {"name": "draft-room", "kind": "draft_room", "path": "draft-room",
        "files": {"<relpath>": "<sha256>"}}]}

``scripts/restore.py`` with::

    class RestoreError(Exception): ...

    def restore_backup_set(
        backup_dir: Path,
        dest_root: Path,
        *,
        lancedb_factory: Callable[[str], Any] | None = None,
        key_provider: Callable[[], tuple[bytes, str]] | None = None,
    ) -> dict

- Decrypts ``app.db.enc`` into ``dest_root/app.db``, copies dir artifacts
  back under ``dest_root/<item path>``, verifies every manifest file digest
  (mismatch -> ``RestoreError``), and for each manifest table entry calls
  ``lancedb_factory(name).checkout(tag)``.
- Returns ``{"verified": bool, "items": [{"name": str, "ok": bool}],
  "sqlite_rows": int}``.

The drill test below exercises backup -> mutate -> restore -> verify
end-to-end with fakes and temp dirs (no real LanceDB, no network).
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import RestoreError, restore_backup_set  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes
MANIFEST_NAME = "backup_manifest.json"
BACKUP_ROWS = 5
MUTATION_ROWS = 3

LANCEDB_MARKER_REL = "chunks/data.marker"
LANCEDB_MARKER_ORIGINAL = b"lancedb-original-bytes"
VAULT_FILE_ORIGINAL = b"vault-file-original"
DRAFT_FILE_ORIGINAL = b"draft-file-original"


class FakeLanceTable:
    """Duck-typed LanceDB table: records version tag + checkout calls."""

    def __init__(self, name):
        self.name = name
        self.tags = []
        self.checkouts = []

    def create_tag(self, tag):
        self.tags.append(tag)
        return tag

    def checkout(self, tag):
        self.checkouts.append(tag)


def _key_provider():
    return (TEST_KEY, "9")


def _row_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def _make_source_tree(root: Path) -> Path:
    src = root / "src"
    (src / "lancedb" / "chunks").mkdir(parents=True)
    (src / "lancedb" / LANCEDB_MARKER_REL).write_bytes(LANCEDB_MARKER_ORIGINAL)
    (src / "vault-7" / "uploads").mkdir(parents=True)
    (src / "vault-7" / "uploads" / "file.txt").write_bytes(VAULT_FILE_ORIGINAL)
    (src / "draft-room").mkdir(parents=True)
    (src / "draft-room" / "in.txt").write_bytes(DRAFT_FILE_ORIGINAL)

    conn = sqlite3.connect(src / "app.db")
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
        for i in range(1, BACKUP_ROWS + 1):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"row-{i}"))
        conn.commit()
    finally:
        conn.close()
    return src


def _mutate_source_tree(src: Path) -> None:
    conn = sqlite3.connect(src / "app.db")
    try:
        for i in range(BACKUP_ROWS + 1, BACKUP_ROWS + MUTATION_ROWS + 1):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"row-{i}"))
        conn.commit()
    finally:
        conn.close()
    (src / "lancedb" / LANCEDB_MARKER_REL).write_bytes(b"mutated-after-backup")
    (src / "vault-7" / "uploads" / "junk.txt").write_bytes(b"junk")
    (src / "draft-room" / "in.txt").write_bytes(b"mutated-after-backup")


def _run_backup(src: Path, out_dir: Path, table: FakeLanceTable):
    import scripts.backup_set as backup_set_module

    monkeypatch = pytest.MonkeyPatch()
    try:
        # settings lives in the script's own import namespace
        # (backend.app.config), so patch the instance the script reads.
        monkeypatch.setattr(
            backup_set_module.settings, "data_dir", src
        )
        manifest_path = create_backup_set(
            out_dir,
            lancedb_dir=src / "lancedb",
            lancedb_tables={"chunks": table},
            vault_dirs=[src / "vault-7"],
            draft_room_dir=src / "draft-room",
            key_provider=_key_provider,
        )
    finally:
        monkeypatch.undo()
    return manifest_path


def test_backup_manifest_binds_all_artifacts_with_version_tags(tmp_path):
    src = _make_source_tree(tmp_path)
    out_dir = tmp_path / "backup1"
    table = FakeLanceTable("chunks")

    manifest_path = _run_backup(src, out_dir, table)
    assert manifest_path == out_dir / MANIFEST_NAME, (
        f"518-C5B MANIFEST PATH: expected {out_dir / MANIFEST_NAME}, got "
        f"{manifest_path}"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("schema") == "ragapp-backup-set/1"
    kinds = {item["kind"] for item in manifest["items"]}
    assert {"sqlite", "lancedb", "vault", "draft_room"} <= kinds, (
        f"518-C5B MANIFEST INCOMPLETE: bound kinds {kinds} lack required "
        "members (sqlite/lancedb/vault/draft_room)"
    )
    lancedb_item = next(
        i for i in manifest["items"] if i["kind"] == "lancedb"
    )
    tables = lancedb_item.get("tables", [])
    assert tables and tables[0]["table"] == "chunks", (
        "518-C5B LANCEDB TABLE NOT BOUND: manifest lancedb item does not "
        f"reference table 'chunks' ({tables})"
    )
    tag = tables[0]["tag"]
    assert tag.startswith("backup-"), (
        f"518-C5B VERSION TAG NAMING: tag {tag!r} does not start with "
        "'backup-'"
    )
    assert tag in table.tags, (
        "518-C5B NO PRE-COPY VERSION TAG: create_backup_set did not call "
        f"table.create_tag({tag!r}) before copying"
    )
    assert (out_dir / "app.db.enc").exists()
    assert LANCEDB_MARKER_REL in lancedb_item.get("files", {}), (
        "518-C5B LANCEDB FILES NOT DIGESTED: manifest carries no sha256 "
        "entry for the copied table file"
    )


def test_restore_drill_recovers_pre_mutation_state(tmp_path):
    src = _make_source_tree(tmp_path)
    out_dir = tmp_path / "backup2"
    backup_table = FakeLanceTable("chunks")
    manifest_path = _run_backup(src, out_dir, backup_table)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tag = next(
        i for i in manifest["items"] if i["kind"] == "lancedb"
    )["tables"][0]["tag"]

    _mutate_source_tree(src)
    assert _row_count(src / "app.db") == BACKUP_ROWS + MUTATION_ROWS

    dest = tmp_path / "restored"
    restored_table = FakeLanceTable("chunks")
    report = restore_backup_set(
        out_dir,
        dest,
        lancedb_factory=lambda name: restored_table,
        key_provider=_key_provider,
    )

    assert report.get("verified") is True, (
        f"518-C5B RESTORE NOT VERIFIED: report={report}"
    )
    assert report.get("items") and all(
        item.get("ok") for item in report["items"]
    ), f"518-C5B RESTORE ITEM FAILED: {report['items']}"
    assert report.get("sqlite_rows") == BACKUP_ROWS, (
        f"518-C5B RESTORE ROW MISMATCH: sqlite_rows={report.get('sqlite_rows')}"
    )

    restored_db = dest / "app.db"
    assert _row_count(restored_db) == BACKUP_ROWS, (
        "518-C5B RESTORE ROW MISMATCH: restored db has "
        f"{_row_count(restored_db)} rows, backup-time state had "
        f"{BACKUP_ROWS} (post-backup mutations leaked into the restore)"
    )
    assert (
        dest / "lancedb" / LANCEDB_MARKER_REL
    ).read_bytes() == LANCEDB_MARKER_ORIGINAL, (
        "518-C5B RESTORE FILE MISMATCH: lancedb file digest/content differs "
        "from backup-time state"
    )
    assert (
        dest / "vault-7" / "uploads" / "file.txt"
    ).read_bytes() == VAULT_FILE_ORIGINAL
    assert (
        dest / "draft-room" / "in.txt"
    ).read_bytes() == DRAFT_FILE_ORIGINAL
    assert restored_table.checkouts == [tag], (
        "518-C5B NO VERSION CHECKOUT: restore did not checkout the backup "
        f"tag {tag!r} on the restored lancedb table "
        f"(checkouts={restored_table.checkouts})"
    )


def test_restore_detects_tampered_artifact(tmp_path):
    src = _make_source_tree(tmp_path)
    out_dir = tmp_path / "backup3"
    _run_backup(src, out_dir, FakeLanceTable("chunks"))

    # Tamper an unencrypted artifact (the copied lancedb file) so the
    # manifest sha256 verification — not AES-GCM authenticity — must catch it.
    tampered = out_dir / "lancedb" / LANCEDB_MARKER_REL
    tampered.write_bytes(b"tampered-backup-bytes")

    # AMEND (CHECK_WRONG, issue-518 trace): the original form
    # `with pytest.raises(RestoreError), ("...")` treated the message string
    # as a second context manager (TypeError under any implementation); the
    # intent is simply that a tampered digest raises RestoreError.
    with pytest.raises(RestoreError):  # 518-C5B TAMPER NOT DETECTED sentinel
        restore_backup_set(
            out_dir,
            tmp_path / "restored-tampered",
            key_provider=_key_provider,
        )
