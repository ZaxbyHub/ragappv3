#!/usr/bin/env python3
"""Embedding dimension migration script — wipe stale LanceDB vectors and queue re-indexing.

Use this script when upgrading to a new embedding model that produces a different vector
dimension (e.g., BGE-M3 768-dim → Harrier 1024-dim). The LanceDB vector store cannot
hold embeddings of mixed dimensions, so the entire index must be cleared and rebuilt.

What this script does
---------------------
1. Detects whether a dimension mismatch exists between the configured
   ``EMBEDDING_DIM`` and the dimension stored in the current LanceDB table.
2. Wipes the LanceDB directory (irreversible without a backup — see ``--dry-run``).
3. Resets all ``indexed`` files in SQLite to ``pending`` so the background
   processor will re-embed them on next startup.

Properties
----------
- **Idempotent**: safe to run on a fresh deployment (no-op when LanceDB is empty).
- **Dry-run mode**: ``--dry-run`` reports what would change without modifying data.
- **Irreversible**: deletes the LanceDB directory.  Back up first if you need rollback.

Usage
-----
    # Dry run — report only, no changes
    python scripts/migrate_embeddings.py --dry-run

    # Live run — wipe LanceDB, reset file statuses
    python scripts/migrate_embeddings.py

    # Specify paths manually (if running outside Docker)
    python scripts/migrate_embeddings.py \\
        --lancedb-path /data/knowledgevault/lancedb \\
        --sqlite-path  /data/knowledgevault/app.db

After running
-------------
Restart the application so the background processor picks up pending files:

    docker compose restart knowledgevault
"""

import argparse
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dimension detection
# ---------------------------------------------------------------------------

def _detect_stored_dim(lancedb_path: Path) -> int | None:
    """Return the embedding dimension stored in the LanceDB chunks table, or None.

    Primary path uses the INSTALLED LanceDB API (issue #512 VECTOR-006) —
    it mirrors production ``VectorStore._get_expected_embedding_dim``:
    connect, open the "chunks" table and read the fixed-size-list length
    from the arrow schema. A raw ``pyarrow.dataset`` read is kept only as
    a fallback for environments without the lancedb package.
    """
    lance_dir = lancedb_path / "chunks.lance"
    if not lance_dir.exists():
        return None

    # Primary: installed LanceDB sync API. pyarrow.dataset cannot parse the
    # table layout written by current lancedb versions, which is exactly how
    # a compatible index used to be mis-detected as "unknown dimension".
    try:
        import lancedb as _ldb  # type: ignore

        db = _ldb.connect(str(lancedb_path))
        if "chunks" not in db.table_names():
            return None
        tbl = db.open_table("chunks")
        schema = tbl.schema
        embedding_field = schema.field("embedding")
        # LanceDB stores fixed-size list embeddings as FixedSizeList type
        if hasattr(embedding_field.type, "list_size"):
            return int(embedding_field.type.list_size)
    except Exception as exc:
        logger.debug("Could not read stored embedding dim via lancedb: %s", exc)

    # Fallback: raw pyarrow dataset read (no lancedb package available).
    try:
        import pyarrow.dataset as ds

        dataset = ds.dataset(str(lance_dir), format="lance")
        schema = dataset.schema
        embedding_field = schema.field("embedding")
        if hasattr(embedding_field.type, "list_size"):
            return embedding_field.type.list_size
    except Exception as exc:
        logger.debug("Could not read stored embedding dim via pyarrow: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Migration steps
# ---------------------------------------------------------------------------

def _wipe_lancedb(lancedb_path: Path, dry_run: bool) -> bool:
    """Delete the LanceDB directory. Returns True if action was (or would be) taken."""
    if not lancedb_path.exists():
        logger.info("LanceDB directory does not exist — nothing to wipe: %s", lancedb_path)
        return False

    if dry_run:
        logger.info("[DRY RUN] Would delete LanceDB directory: %s", lancedb_path)
        return True

    shutil.rmtree(lancedb_path)
    lancedb_path.mkdir(parents=True, exist_ok=True)
    logger.info("Deleted and recreated LanceDB directory: %s", lancedb_path)
    return True


def _reset_file_statuses(sqlite_path: Path, dry_run: bool) -> int:
    """Reset all indexed files to pending. Returns the count of affected rows."""
    if not sqlite_path.exists():
        logger.warning("SQLite database not found at %s — skipping status reset.", sqlite_path)
        return 0

    conn = sqlite3.connect(str(sqlite_path))
    try:
        # Check the files table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        )
        if cursor.fetchone() is None:
            logger.info("'files' table not found — nothing to reset.")
            return 0

        cursor = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status = 'indexed'"
        )
        indexed_count = cursor.fetchone()[0]

        if indexed_count == 0:
            logger.info("No indexed files found — status reset is a no-op.")
            return 0

        if dry_run:
            logger.info(
                "[DRY RUN] Would reset %d indexed file(s) to 'pending'.", indexed_count
            )
            return indexed_count

        conn.execute(
            """
            UPDATE files
            SET status = 'pending',
                chunk_count = 0,
                processed_at = NULL,
                modified_at = CURRENT_TIMESTAMP
            WHERE status = 'indexed'
            """
        )
        conn.commit()
        logger.info("Reset %d file(s) from 'indexed' to 'pending'.", indexed_count)
        return indexed_count

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_migration(
    lancedb_path: Path,
    sqlite_path: Path,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Run the embedding migration.

    Args:
        lancedb_path: Path to the LanceDB directory.
        sqlite_path: Path to the SQLite database file.
        dry_run: Report changes without writing.
        force: Skip the dimension-mismatch check and always migrate.

    Returns:
        Number of files that were (or would be) reset to 'pending'.
    """
    print()
    print("=" * 60)
    print("  KnowledgeVault — Embedding Dimension Migration")
    print("=" * 60)
    print(f"  LanceDB path : {lancedb_path}")
    print(f"  SQLite path  : {sqlite_path}")
    print(f"  Dry run      : {dry_run}")
    print("=" * 60)
    print()

    # ── Step 1: Detect dimension mismatch ──────────────────────────────────
    stored_dim = _detect_stored_dim(lancedb_path)

    if stored_dim is None:
        if lancedb_path.exists() and any(lancedb_path.iterdir()):
            # VECTOR-006 (issue #512): an undetectable dimension on a
            # NON-EMPTY index used to be treated as "assuming migration
            # needed" and wiped an index that may have been perfectly
            # compatible. Refuse instead: nothing destructive runs until an
            # operator has diagnosed the index (or explicitly --force'd).
            if force:
                print(
                    "WARNING: --force set — proceeding despite undetectable "
                    "stored dimension.\n"
                )
            else:
                print(
                    "ERROR: Could not detect the stored embedding dimension of "
                    "the existing LanceDB index at:\n"
                    f"  {lancedb_path}\n"
                    "The index directory is not empty, so NO wipe was performed "
                    "and NO file statuses were reset.\n"
                    "Diagnose the index before re-running: open it with the "
                    "installed lancedb and inspect the 'chunks' table schema "
                    "(e.g. lancedb.connect(path).open_table('chunks').schema). "
                    "If the index is genuinely stale, re-run with --force.\n"
                )
                raise SystemExit(3)
        else:
            logger.info("LanceDB directory is empty or absent — no migration needed.")
            if not force:
                print("\nNothing to migrate. If you want to force a reset, use --force.\n")
                return 0

    try:
        # Load settings to get configured embedding_dim
        ROOT = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(ROOT / "backend"))
        from app.config import settings  # noqa: PLC0415
        configured_dim = settings.embedding_dim
    except Exception as exc:
        logger.warning("Could not load settings (EMBEDDING_DIM): %s", exc)
        configured_dim = None

    if stored_dim and configured_dim and stored_dim == configured_dim and not force:
        print(
            f"Embedding dimensions match ({stored_dim}-dim). No migration needed.\n"
            "To force a reset anyway, use --force.\n"
        )
        return 0

    if stored_dim and configured_dim:
        print(
            f"Dimension mismatch detected:\n"
            f"  Stored in LanceDB : {stored_dim}-dim\n"
            f"  Configured        : {configured_dim}-dim\n"
            f"  Action            : Wipe LanceDB and reset file statuses.\n"
        )
    elif force:
        print("Running forced migration (--force flag set).\n")
    else:
        print("Could not detect stored dimension. Proceeding with migration.\n")

    if not dry_run:
        print(
            "WARNING: This will permanently delete all LanceDB vectors.\n"
            "         Back up /your/data/lancedb before continuing.\n"
        )

    # ── Step 2: Wipe LanceDB ───────────────────────────────────────────────
    print("[1/2] Wiping LanceDB vector index...")
    _wipe_lancedb(lancedb_path, dry_run=dry_run)

    # ── Step 3: Reset file statuses ────────────────────────────────────────
    print("[2/2] Resetting file statuses to 'pending'...")
    reset_count = _reset_file_statuses(sqlite_path, dry_run=dry_run)

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    if dry_run:
        print(
            f"[DRY RUN] Migration summary:\n"
            f"  LanceDB wipe         : {'yes' if lancedb_path.exists() else 'no (already empty)'}\n"
            f"  Files to reset       : {reset_count}\n"
            f"\nRe-run without --dry-run to apply changes."
        )
    else:
        print(
            f"Migration complete:\n"
            f"  LanceDB wiped        : yes\n"
            f"  Files reset          : {reset_count}\n"
            f"\nNext step: restart the application to begin re-indexing.\n"
            f"  docker compose restart knowledgevault\n"
            f"\nThe background processor will re-embed all {reset_count} file(s) automatically."
        )
    print()
    return reset_count


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Wipe stale LanceDB embeddings and reset file statuses for "
            "embedding model migration (e.g., BGE-M3 768-dim → Harrier 1024-dim)."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without making any modifications.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip dimension-mismatch detection and force migration.",
    )
    parser.add_argument(
        "--lancedb-path",
        type=Path,
        default=None,
        help="Path to LanceDB directory. Defaults to settings.lancedb_path.",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="Path to SQLite database file. Defaults to settings.sqlite_path.",
    )
    args = parser.parse_args()

    # Resolve paths
    lancedb_path = args.lancedb_path
    sqlite_path = args.sqlite_path

    if lancedb_path is None or sqlite_path is None:
        ROOT = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(ROOT / "backend"))
        try:
            from app.config import settings  # noqa: PLC0415
            if lancedb_path is None:
                lancedb_path = settings.lancedb_path
            if sqlite_path is None:
                sqlite_path = settings.sqlite_path
        except Exception as exc:
            print(
                f"ERROR: Could not load settings: {exc}\n"
                "Pass --lancedb-path and --sqlite-path explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)

    run_migration(
        lancedb_path=lancedb_path,
        sqlite_path=sqlite_path,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    main()
