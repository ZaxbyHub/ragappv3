"""Remove expired backups: legacy single-file ``db_*_k*.enc`` names AND
backup-set directories (a directory containing ``backup_manifest.json``,
what scripts/backup_set.py produces — issue #727 / T1-28-K-04: the
documented pruner previously matched only the legacy names, so no set ever
left the backups directory).

Before a set directory is removed, the live ``backup-*`` table tags its
manifest names are released on the live LanceDB store (resolved through
the settings singleton at call time), so pruned sets stop pinning table
versions and the app's periodic ``optimize()`` keeps working past the
retention window (#727 / T1-28-S2-04). Tag release FAILS CLOSED: a
missing live store or a confirmed-absent table/tag is tolerated, but any
other error (I/O, permissions, unknown) aborts removal of that set and
keeps the directory — with its manifest — available for a retry. A
malformed or unsupported manifest also skips the set instead of deleting
evidence, and a set candidate that is a symlink or not a directory is
never followed.

``--release-unreferenced-tags`` (opt-in) additionally deletes live
``backup-*`` tags that no REMAINING set manifest in the directory
references. The census is fail-closed — every remaining candidate
manifest must read and validate, or the whole reconciliation phase is
skipped with an error — and references are keyed ``(table, tag)``. It is
opt-in because sets may legitimately live in several directories; a
single directory's view cannot safely infer orphanhood automatically.
"""

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))
# backend.app.config imports ``app.*`` absolutely (config.py L13), which
# only resolves with backend/ itself on the path (issue #727 / T1-28-K-01).
sys.path.append(str(ROOT / "backend"))

logger = logging.getLogger(__name__)

MANIFEST_NAME = "backup_manifest.json"
SUPPORTED_SCHEMAS = ("ragapp-backup-set/1",)
TAG_PREFIX = "backup-"


class _SetSkipped(Exception):
    """Internal: this set must not be removed (reason carries why)."""


def _is_backup_set_dir(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        os.readlink(path)
        # readlink succeeded but is_symlink() is false: a Windows
        # junction/reparse point — never a candidate (the sweep loop
        # skips it with a logged error before any tag release).
        return False
    except (OSError, ValueError):
        pass
    return path.is_dir() and (path / MANIFEST_NAME).is_file()


def _load_manifest(set_dir: Path) -> dict:
    """Load and schema-validate a set manifest; raise on anything that is
    not a recognizable backup set (fail closed — never delete a directory
    whose tags we cannot know)."""
    manifest_path = set_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _SetSkipped(f"unreadable manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") not in SUPPORTED_SCHEMAS:
        raise _SetSkipped(
            f"unsupported manifest schema {manifest.get('schema')!r}"
            if isinstance(manifest, dict)
            else "manifest is not an object"
        )
    return manifest


def _manifest_tags(manifest: dict) -> list:
    """[(table, tag)] the manifest binds, ``backup-``-prefixed only."""
    pairs = []
    for item in manifest.get("items", []):
        if item.get("kind") != "lancedb":
            continue
        for table_entry in item.get("tables", []):
            tag = str(table_entry.get("tag", ""))
            if tag.startswith(TAG_PREFIX):
                pairs.append((str(table_entry.get("table", "")), tag))
    return pairs


def _live_lancedb_dir() -> Path:
    # Lazy import: --help and legacy-file pruning stay dependency-free.
    from backend.app.config import settings

    return Path(settings.lancedb_path)


def _tag_names(table) -> list:
    listed = table.tags.list()
    # lancedb >= 0.39 returns a dict keyed by tag; older shapes return an
    # iterable of tag names.
    return list(listed.keys()) if isinstance(listed, dict) else list(listed)


def _release_tags(pairs) -> None:
    """Delete ``(table, tag)`` pairs on the live store.

    Tolerated (proceed): no live lancedb directory; table absent; tag
    already absent. Anything else raises (the caller keeps the set).
    """
    lancedb_dir = _live_lancedb_dir()
    if not lancedb_dir.exists():
        logger.info("no live lancedb directory at %s; nothing to release", lancedb_dir)
        return
    import lancedb

    db = lancedb.connect(str(lancedb_dir))
    for table_name, tag in pairs:
        try:
            table = db.open_table(table_name)
        except Exception as exc:  # noqa: BLE001 — absence classes vary
            message = str(exc).lower()
            if "not found" in message or "does not exist" in message or "no table" in message:
                logger.info(
                    "table %r absent from the live store; tag %r release "
                    "not needed",
                    table_name,
                    tag,
                )
                continue
            raise _SetSkipped(
                f"could not open live table {table_name!r} to release tag "
                f"{tag!r}: {exc}"
            ) from exc
        try:
            names = _tag_names(table)
        except Exception as exc:  # noqa: BLE001 — unknown errors abort the set
            raise _SetSkipped(
                f"could not list tags on live table {table_name!r}: {exc}"
            ) from exc
        if tag not in names:
            logger.info("tag %r already absent from live table %r", tag, table_name)
            continue
        try:
            table.tags.delete(tag)
        except Exception as exc:  # noqa: BLE001 — unknown errors abort
            raise _SetSkipped(
                f"could not release tag {tag!r} on live table {table_name!r}: {exc}"
            ) from exc
        logger.info("released live tag %r on table %r", tag, table_name)


def cleanup_backups(
    directory: Path,
    retention_days: int,
    *,
    release_orphan_tags: bool = False,
) -> list:
    """Remove expired legacy backup files and backup-set directories.

    Returns the list of removed paths. Removal happens only after the
    set's live tags were released (or confirmed absent); an unreleasable
    set is kept and the error logged — the sweep continues with the rest.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be >= 0")
    directory = Path(directory)
    now = datetime.now(timezone.utc)
    retention_delta = timedelta(days=retention_days)
    removed: list = []

    # Legacy single-file backups (pre-set format).
    for path in directory.glob("db_*_k*.enc"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except FileNotFoundError:
            continue
        if now - mtime > retention_delta:
            try:
                path.unlink()
                removed.append(path)
            except (OSError, PermissionError, FileNotFoundError):
                # File may be locked or already deleted
                continue

    # Backup-set directories: aged by the MANIFEST's mtime (the set was
    # finished when the manifest landed), tags released before removal.
    candidates = sorted(directory.iterdir())
    set_dirs = []
    skipped_links: list = []
    for cand in candidates:
        if _is_backup_set_dir(cand):
            if ".partial-" in cand.name:
                # The producer's in-flight staging directory carries its
                # manifest before the commit rename; pruning it here would
                # release the in-flight run's tags and delete the tree the
                # producer is still writing (swarm review pr1026 PRR-009).
                logger.info(
                    "skipping in-flight backup staging directory %s",
                    cand,
                )
                continue
            set_dirs.append(cand)
        elif (cand / MANIFEST_NAME).is_file() and not cand.is_file():
            # A manifest-bearing entry that failed the candidate check:
            # a symlink or junction. Skip it with a logged error BEFORE
            # any manifest load or tag release (#728 review finding) —
            # never prune through a link.
            skipped_links.append(cand)
            logger.error(
                "skipping backup-set candidate %s: it is a symlink or "
                "junction; pruning never follows links — remove or "
                "replace it manually",
                cand,
            )
    expired = []
    for set_dir in set_dirs:
        manifest_path = set_dir / MANIFEST_NAME
        try:
            mtime = datetime.fromtimestamp(
                manifest_path.stat().st_mtime, tz=timezone.utc
            )
        except FileNotFoundError:
            continue
        if now - mtime > retention_delta:
            expired.append(set_dir)

    for set_dir in expired:
        try:
            manifest = _load_manifest(set_dir)
            pairs = _manifest_tags(manifest)
        except _SetSkipped as exc:
            logger.error(
                "keeping expired backup set %s: %s (fix or remove it "
                "manually)",
                set_dir,
                exc,
            )
            continue
        try:
            _release_tags(pairs)
        except _SetSkipped as exc:
            logger.error(
                "keeping expired backup set %s: %s (retry after resolving)",
                set_dir,
                exc,
            )
            continue
        try:
            shutil.rmtree(set_dir)
        except OSError as exc:
            # One candidate's removal failing must not abort the sweep
            # (#728 review finding); the set stays for a retry.
            logger.error(
                "keeping expired backup set %s: removal failed (%s); its "
                "live tags were ALREADY RELEASED, so the retained copy is "
                "no longer pinned — retry the removal or delete it "
                "manually",
                set_dir,
                exc,
            )
            continue
        removed.append(set_dir)

    if release_orphan_tags:
        if skipped_links:
            # A skipped linked set's manifest never entered the census,
            # so tags it references would read as orphans — abort the
            # whole reconciliation phase rather than risk deleting live
            # tags (implementation-review round 2 question).
            logger.error(
                "orphan-tag reconciliation SKIPPED: %d candidate(s) are "
                "symlinks/junctions whose manifests cannot be counted "
                "(%s); remove or replace the links, then retry",
                len(skipped_links),
                ", ".join(str(p) for p in skipped_links),
            )
        else:
            _release_unreferenced_tags(
                set_dirs=[p for p in set_dirs if p not in removed]
            )
    return removed


def _release_unreferenced_tags(*, set_dirs) -> None:
    """Opt-in orphan reconciliation: delete live ``backup-*`` tags no
    remaining set manifest in THIS directory references.

    Fail-closed census: every remaining candidate manifest must load and
    validate, otherwise the whole phase is skipped with an error (a
    preserved-but-unreadable set must never have its tags orphaned by us).
    """
    referenced = set()
    for set_dir in set_dirs:
        try:
            manifest = _load_manifest(set_dir)
        except _SetSkipped as exc:
            logger.error(
                "orphan-tag reconciliation SKIPPED: remaining set %s has "
                "an unusable manifest (%s); resolve it first",
                set_dir,
                exc,
            )
            return
        referenced.update(_manifest_tags(manifest))

    lancedb_dir = _live_lancedb_dir()
    if not lancedb_dir.exists():
        logger.info("no live lancedb directory at %s; no orphans to release", lancedb_dir)
        return
    import lancedb

    db = lancedb.connect(str(lancedb_dir))
    for table_name in db.table_names():
        table = db.open_table(table_name)
        for tag in [n for n in _tag_names(table) if n.startswith(TAG_PREFIX)]:
            if (table_name, tag) in referenced:
                continue
            try:
                table.tags.delete(tag)
                logger.info(
                    "released unreferenced live tag %r on table %r",
                    tag,
                    table_name,
                )
            except Exception as exc:  # noqa: BLE001 — per-tag best effort
                logger.error(
                    "could not release unreferenced tag %r on table %r: %s",
                    tag,
                    table_name,
                    exc,
                )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="Cleanup old encrypted backups")
    parser.add_argument("--directory", type=Path, default=Path("backups"))
    parser.add_argument("--retention", type=int, default=30)
    parser.add_argument(
        "--release-unreferenced-tags",
        action="store_true",
        help="Also delete live backup-* tags no remaining set manifest in "
        "the directory references (opt-in: sets may live in several "
        "directories)",
    )
    args = parser.parse_args()
    if args.retention < 0:
        parser.error("--retention must be >= 0")

    args.directory.mkdir(parents=True, exist_ok=True)
    removed = cleanup_backups(
        args.directory,
        args.retention,
        release_orphan_tags=args.release_unreferenced_tags,
    )
    if removed:
        for path in removed:
            print(f"Removed backup: {path}")


if __name__ == "__main__":
    main()
