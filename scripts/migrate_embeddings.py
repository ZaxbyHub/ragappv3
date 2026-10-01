#!/usr/bin/env python3
"""Embedding dimension migration script — reset file statuses and wipe stale LanceDB vectors.

Use this script when upgrading to a new embedding model that produces a different vector
dimension (e.g., BGE-M3 768-dim -> Harrier 1024-dim). The LanceDB vector store cannot
hold embeddings of mixed dimensions, so the entire index must be cleared and rebuilt.

What this script does
---------------------
1. Detects whether a dimension mismatch exists between the configured
   ``EMBEDDING_DIM`` and the dimension stored in the current LanceDB table.
2. Resets all ``indexed``/``partial`` files in SQLite to ``pending`` (with
   ``phase='queued'`` so every recovery sweep re-adopts them) so the
   background processor will re-embed them on next startup.
3. Wipes the LanceDB directory (irreversible without a backup — see ``--dry-run``).

The SQLite reset runs BEFORE the LanceDB wipe (issue #694): the wipe is the
only irreversible step, so a crash or lock failure before it leaves the old
vectors intact as the rollback source instead of stranding ``indexed`` rows
over an empty index.

Properties
----------
- **Idempotent / self-repairing**: safe to run on a fresh deployment (no-op
  when LanceDB is empty and no rows claim vectors). A re-run after an
  interrupted prior run DETECTS the inconsistency (present-but-empty index,
  rows still ``indexed``/``partial``) and repairs it by resetting those rows
  for re-indexing instead of reporting "Nothing to migrate". An ABSENT
  directory is treated as a clean deployment (or a wrong ``--lancedb-path``)
  and keeps the plain no-op even when rows claim vectors — pass an explicit
  correct path to repair that state.
- **Repair mode** (the interrupted-run path above) never wipes data the
  index no longer has, so it neither loads settings (the exit-4 refusal does
  not apply) nor consults ``--force`` — it only resets rows.
- **Dry-run mode**: ``--dry-run`` reports what would change without modifying data.
- **Irreversible**: deletes the LanceDB directory.  Back up first if you need rollback.
- **Refuses instead of destroying** when it cannot prove a migration is safe:
  exit code 3 = stored dimension undetectable on a non-empty index,
  exit code 4 = configured dimension unreadable (settings load failed),
  exit code 5 = another process holds a write lock on the SQLite database
  (stop the application first) or the database is unreadable/corrupt,
  exit code 6 = the SQLite database is unreadable/corrupt during repair
  detection (restore it from your backup).
  ``--force`` overrides exits 3 and 4 only; the lock/corruption refusals
  (5 and 6) always stop the run.

Usage
-----
Stop the application first (the script refuses while another writer holds
the database)::

    docker compose stop knowledgevault

    # Dry run — report only, no changes
    python scripts/migrate_embeddings.py --dry-run

    # Live run — reset file statuses, wipe LanceDB
    python scripts/migrate_embeddings.py

    # Specify paths manually (if running outside Docker)
    python scripts/migrate_embeddings.py \\
        --lancedb-path /data/knowledgevault/lancedb \\
        --sqlite-path  /data/knowledgevault/app.db

After running
-------------
Start the application so the background processor picks up pending files::

    docker compose start knowledgevault

If a migration run is interrupted, re-run THIS SCRIPT before restarting the
application: after the status reset, an app boot against the still-old-dim
index would fail every re-embed (the vector store rejects mismatched
dimensions) and leave files in ``error``/retry states no recovery sweep
re-adopts. If the re-run itself refuses with exit code 3 (a partially
deleted index — e.g. the interruption landed mid-wipe), the deletion is
incomplete: re-run with ``--force`` to finish it.
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


class MigrationRefused(Exception):
    """A safety precondition failed; the migration must not proceed.

    Raised for refusals that ``run_migration`` callers may want to catch as
    ordinary exceptions (issue #694): a concurrent writer holding the SQLite
    database. ``main()`` maps this to exit code 5.
    """


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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table`` (empty when the table is absent)."""
    # nosec B608 - table is a static literal at every call site, never user input
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _count_rows_claiming_vectors(sqlite_path: Path) -> int:
    """Count files rows whose status claims live vectors (``indexed``/``partial``).

    Used to detect the state an interrupted prior run leaves behind: an
    empty/absent LanceDB index with rows still claiming vectors is
    inconsistent and must be repaired, not reported as "nothing to migrate".

    Raises SystemExit(6) when the database file cannot be read at all — an
    unreadable/corrupt SQLite file is a refusal, not a zero count (issue
    #694 implementation review: silently treating it as "no stranded rows"
    or crashing with a bare traceback both violate the exit contract).
    """
    if not sqlite_path.exists():
        return 0
    conn = sqlite3.connect(str(sqlite_path))
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        )
        if cursor.fetchone() is None:
            return 0
        cursor = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status IN ('indexed', 'partial')"
        )
        return int(cursor.fetchone()[0])
    except sqlite3.OperationalError as exc:
        # Mirror the writer pre-flight's ordering: a lock (rare for a plain
        # read — writers must hold EXCLUSIVE) is a live-writer refusal, not
        # corruption.
        raise MigrationRefused(
            f"Another process appears to hold a write lock on the SQLite "
            f"database ({exc}): {sqlite_path}\n"
            "Is the application still running? Stop it first, e.g.:\n"
            "  docker compose stop knowledgevault\n"
            "Then re-run this migration."
        ) from exc
    except sqlite3.DatabaseError as exc:
        print(
            f"ERROR: The SQLite database is unreadable or corrupt: "
            f"{sqlite_path}\n  {exc}\n"
            "Repair or restore it (from your backup) before re-running.\n",
            file=sys.stderr,
        )
        raise SystemExit(6) from exc
    finally:
        conn.close()


def _assert_no_active_writer(sqlite_path: Path) -> None:
    """Refuse to touch stores another process is writing (issue #694).

    A short-lived ``BEGIN IMMEDIATE`` probe: an active writer (typically the
    running application) holds the database's write lock, and migrating
    concurrently with it cannot end well for either side. Raises
    ``MigrationRefused`` (an ordinary Exception — ``main()`` maps it to exit
    code 5) instead of ``SystemExit`` so programmatic callers of
    ``run_migration`` can catch it with ``except Exception``. A database
    that cannot be read at all (corrupt file) raises the same exception
    with a restore-the-backup message instead of the stop-the-app advice.
    """
    if not sqlite_path.exists():
        return
    conn = sqlite3.connect(str(sqlite_path), timeout=2.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise MigrationRefused(
            f"Another process appears to hold a write lock on the SQLite "
            f"database ({exc}): {sqlite_path}\n"
            "Is the application still running? Stop it first, e.g.:\n"
            "  docker compose stop knowledgevault\n"
            "Then re-run this migration."
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise MigrationRefused(
            f"The SQLite database is unreadable or corrupt ({exc}): "
            f"{sqlite_path}\n"
            "Repair or restore it (from your backup) before re-running."
        ) from exc
    finally:
        conn.close()


def _reset_file_statuses(sqlite_path: Path, dry_run: bool) -> int:
    """Reset all indexed/partial files to pending. Returns the count of affected rows.

    The predicate covers ``partial`` too (issue #513 status, fixed by issue
    #694): after the whole index is wiped, a ``partial`` file's non-zero
    ``chunk_count`` claims vectors it no longer has. Optional progress/health
    columns (``phase``, ``partial_embeddings``, ``chunks_failed``,
    ``error_message``) are restored to their queue-entry values only when
    they exist, so ancient schemas without them still reset cleanly.

    Raises ``MigrationRefused`` (→ exit 5 via ``main()``) if a writer acquires
    the database lock after the writer probe but before this UPDATE (the
    probe-to-reset window) — the same refusal contract as the probe, instead
    of a bare traceback. (The read statements above the UPDATE are outside
    this handler; in live runs the writer probe pre-empts an unreadable
    database before the reset is reached.)
    """
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
            "SELECT COUNT(*) FROM files WHERE status IN ('indexed', 'partial')"
        )
        resettable_count = cursor.fetchone()[0]

        if resettable_count == 0:
            logger.info("No indexed/partial files found — status reset is a no-op.")
            return 0

        if dry_run:
            logger.info(
                "[DRY RUN] Would reset %d indexed/partial file(s) to 'pending'.",
                resettable_count,
            )
            return resettable_count

        # Column fragments are literal SQL (no interpolated values), assembled
        # per the schema that actually exists on this database.
        set_fragments = [
            "status = 'pending'",
            "chunk_count = 0",
            "processed_at = NULL",
            "modified_at = CURRENT_TIMESTAMP",
        ]
        columns = _table_columns(conn, "files")
        if "phase" in columns:
            # Restore the queue signal both recovery modes match: the lease-mode
            # boot enqueue takes any non-error phase, the legacy sweep needs
            # exactly status='pending' AND phase='queued' (background_tasks.py).
            set_fragments.append("phase = 'queued'")
        if "partial_embeddings" in columns:
            set_fragments.append("partial_embeddings = 0")
        if "chunks_failed" in columns:
            set_fragments.append("chunks_failed = 0")
        if "error_message" in columns:
            set_fragments.append("error_message = NULL")

        try:
            conn.execute(
                "UPDATE files SET "
                + ", ".join(set_fragments)
                + " WHERE status IN ('indexed', 'partial')"
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            # The writer probe passed moments ago; a writer that acquired the
            # lock in the probe-to-reset window surfaces here (PRR-001, PR
            # #831 review). Same refusal contract as the probe — never a bare
            # traceback, and the wipe below never runs.
            raise MigrationRefused(
                f"Another process acquired a write lock on the SQLite "
                f"database during the migration ({exc}): {sqlite_path}\n"
                "Is the application still running? Stop it first, e.g.:\n"
                "  docker compose stop knowledgevault\n"
                "Then re-run this migration."
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise MigrationRefused(
                f"The SQLite database is unreadable or corrupt ({exc}): "
                f"{sqlite_path}\n"
                "Repair or restore it (from your backup) before re-running."
            ) from exc
        logger.info(
            "Reset %d file(s) from 'indexed'/'partial' to 'pending'.",
            resettable_count,
        )
        return resettable_count

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

    Raises:
        SystemExit: code 3 when the stored dimension is undetectable on a
            non-empty index without ``--force``; code 4 when the configured
            dimension is unreadable (settings load failure) without
            ``--force``; code 6 when the SQLite database is unreadable or
            corrupt during repair detection.
        MigrationRefused: another process holds a write lock on the SQLite
            database, or the database is unreadable/corrupt at the writer
            probe (``main()`` maps this to exit code 5).
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
    repair_mode = False

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
            # Repair detection is scoped to a PRESENT-but-empty index
            # (issue #694 AC2 wording): that is exactly the directory state
            # `_wipe_lancedb` leaves behind (rmtree + mkdir). An ABSENT
            # directory is indistinguishable from a wrong --lancedb-path or
            # a never-indexed deployment, so it keeps the clean no-op.
            stranded = (
                _count_rows_claiming_vectors(sqlite_path) if lancedb_path.exists() else 0
            )
            if stranded > 0:
                # Issue #694: an empty index with rows still claiming vectors
                # is the state an interrupted prior run leaves behind. Repair
                # it: reset those rows so re-embedding can happen, instead of
                # reporting "Nothing to migrate" over an inconsistency
                # SQLite can still see.
                repair_mode = True
                print(
                    f"Detected an inconsistent state: the LanceDB index at\n"
                    f"  {lancedb_path}\n"
                    f"is empty, but {stranded} file row(s) still claim live "
                    "vectors ('indexed'/'partial').\n"
                    "Repairing: resetting those rows to 'pending' for "
                    "re-indexing.\n"
                )
            elif not force:
                logger.info("LanceDB directory is empty or absent — no migration needed.")
                print("\nNothing to migrate. If you want to force a reset, use --force.\n")
                return 0

    settings_load_failed = False
    configured_dim = None
    if not repair_mode:
        try:
            # Load settings to get configured embedding_dim
            ROOT = Path(__file__).resolve().parents[1]
            sys.path.insert(0, str(ROOT / "backend"))
            from app.config import settings  # noqa: PLC0415
            configured_dim = settings.embedding_dim
        except Exception as exc:
            logger.warning("Could not load settings (EMBEDDING_DIM): %s", exc)
            configured_dim = None
            settings_load_failed = True

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
                f"  Action            : Reset file statuses, then wipe LanceDB.\n"
            )
        elif force:
            print("Running forced migration (--force flag set).\n")
        elif settings_load_failed:
            # Issue #694: a settings-load failure must never be equivalent to
            # a confirmed dimension match (or mismatch) — the destructive
            # path requires --force whenever the configured dimension cannot
            # be determined. The STORED dimension was readable above.
            print(
                "ERROR: Could not load the configured embedding dimension "
                "(settings import failed — see the warning above).\n"
                "The stored dimension IS readable"
                + (f" ({stored_dim}-dim)" if stored_dim else "")
                + ", but refusing to migrate without knowing the configured "
                "dimension.\n"
                "Fix the settings problem (or pass --force to migrate "
                "anyway).\n"
            )
            raise SystemExit(4)
        else:
            # Defensive: unreachable while the branches above cover every
            # state; refuse rather than proceed silently.
            print(
                "ERROR: Could not determine the embedding dimensions.\n"
                "Refusing to migrate. Re-run with --force to override.\n"
            )
            raise SystemExit(4)

    if not dry_run:
        print(
            "WARNING: This will permanently delete all LanceDB vectors.\n"
            "         Back up /your/data/lancedb before continuing.\n"
        )
        # Issue #694: refuse to race a live writer. Probe BEFORE the first
        # destructive step (the SQLite reset — the wipe follows it).
        _assert_no_active_writer(sqlite_path)

    # ── Step 2: Reset file statuses ────────────────────────────────────────
    # The reset runs FIRST (issue #694): if it fails (locked DB, crash), the
    # LanceDB index is still intact as the rollback source and the rows keep
    # claiming vectors they still have. A crash after the reset but before
    # the wipe is repaired by re-running the script.
    print("[1/2] Resetting file statuses to 'pending'...")
    reset_count = _reset_file_statuses(sqlite_path, dry_run=dry_run)

    # ── Step 3: Wipe LanceDB ───────────────────────────────────────────────
    print("[2/2] Wiping LanceDB vector index...")
    _wipe_lancedb(lancedb_path, dry_run=dry_run)

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
            f"  Files reset          : {reset_count}\n"
            f"  LanceDB wiped        : yes\n"
            f"\nNext step: start the application to begin re-indexing.\n"
            f"  docker compose start knowledgevault\n"
            f"\nThe background processor will re-embed all {reset_count} file(s) automatically."
        )
    print()
    return reset_count


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Reset file statuses and wipe stale LanceDB embeddings for "
            "embedding model migration (e.g., BGE-M3 768-dim -> Harrier 1024-dim)."
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

    try:
        run_migration(
            lancedb_path=lancedb_path,
            sqlite_path=sqlite_path,
            dry_run=args.dry_run,
            force=args.force,
        )
    except MigrationRefused as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(5)


if __name__ == "__main__":
    main()
