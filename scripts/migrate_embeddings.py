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
3. Replaces the LanceDB index: the previous index directory is PRESERVED as a
   timestamped rollback sibling (``<name>.rollback-YYYYMMDD-HHMMSS``) and the
   live directory is recreated empty (issue #1094).

The SQLite reset runs BEFORE the LanceDB replacement (issue #694): a crash or
lock failure before it leaves everything untouched, and the replacement itself
preserves the old vectors as the rollback directory instead of deleting them.
Rollback directories accumulate next to the live index until you remove them
(``scripts/cleanup_backups.py`` does NOT manage them); rebuilding temporarily
needs space for BOTH indexes.

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
- **Recoverable by default** (issue #1094): the destructive step preserves the
  previous index as a ``<name>.rollback-YYYYMMDD-HHMMSS`` sibling instead of
  deleting it. If the rename itself fails (locked files, permissions), the run
  REFUSES: the SQLite status reset has already committed at that point, but the
  index is intact — free the lock and re-run (the run is the documented
  interrupted-run repair). The rename does not make concurrent application
  writes safe: stop the application first, as shown under Usage.
- **Refuses instead of destroying** when it cannot prove a migration is safe:
  exit code 3 = stored dimension undetectable on a non-empty index,
  exit code 4 = configured dimension unreadable (settings load failed),
  exit code 5 = another process holds a write lock on the SQLite database
  (stop the application first), the database is unreadable/corrupt, or the
  claimed-id identity probe itself could not read the SQLite database
  (a probe failure is a refusal, never a pass),
  exit code 6 = the SQLite database is unreadable/corrupt during repair
  detection (restore it from your backup),
  exit code 7 = neither the LanceDB path nor the SQLite path exists (wrong
  paths — for example container-side DATA_DIR paths used from the host),
  exit code 8 = the application SQLite database is unusable for a reset/wipe
  (dry runs included): missing, no ``files`` table, a ``files`` table without
  the application's core columns, an obvious backup copy (rotated and
  ``.copy`` names such as ``app.db.bak.1``, a file directly inside a ``backups/``
  directory, and a path that RESOLVES to one included), or a
  database whose vector-claiming rows share
  no file id with a non-empty index's chunks — each would let the wipe
  destroy vectors with no usable rollback source. These are heuristics,
  not proof of identity: a byte-identical copy of the live database under
  a live-looking name, and another deployment's database whose
  autoincrement file ids overlap this index's (both number files from 1),
  pass them — point --sqlite-path at the live file. A database
  where NO rows claim vectors gets a loud WARNING instead (live run and
  dry-run): that state is indistinguishable from the documented
  reset-then-crash repair re-run.
  ``--force`` overrides exits 3 and 4 only; the lock/corruption/wrong-path
  refusals (5, 6, 7 and 8) always stop the run.

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
re-adopts. Because the replacement renames (rather than deletes) the index,
there is no partially-deleted state: the previous index survives either under
its live name or under its rollback name. If the interruption landed between
the rename and the recreation, the rollback directory printed by that run is
the recovery copy — recreate the live directory (or rename the rollback
directory back) and re-run. Renaming the rollback directory back restores the
INDEX only; the SQLite rows were already reset, so re-running this script
afterwards is the documented completion path.
"""

import argparse
import logging
import re
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from datetime import datetime
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


#: Name tokens that mark a backup copy (split on . - _ ~ and whitespace).
_BACKUP_TOKENS = frozenset({"bak", "backup", "orig", "copy"})

#: Parent-directory tokens that mark a backup location. Deliberately narrower
#: than the file-name set (no ``bak``/``orig``/``copy``): a live data
#: directory's name must not trip it, and short tokens could collide with
#: generated directory names (batch-11 review).
_BACKUP_DIR_TOKENS = frozenset({"backup", "backups"})


def _looks_like_backup(lower_name: str) -> bool:
    """Whether a lower-cased file name looks like a backup copy.

    Token-based so rotated and stamped forms are caught too — ``app.db.bak``,
    ``app.db.bak.1``, ``app.db.backup-2024``, ``app_backup.db``,
    ``app.db.copy``, ``app.db.orig~`` (pr1081-20261106 batch-7/batch-11
    reviews: exact-suffix matching let rotated names through) — plus the
    ``backup``/``copy of`` prefixes and editor ``~`` copies.
    """
    if lower_name.startswith(("backup", "copy of")) or lower_name.endswith("~"):
        return True
    return any(tok in _BACKUP_TOKENS for tok in re.split(r"[.\-_~\s]+", lower_name))


def _dir_looks_like_backup(lower_dir_name: str) -> bool:
    """Whether the database's IMMEDIATE parent directory is a backup location
    (``backups/app.db``, ``db-backup/app.db``; batch-11 review)."""
    if lower_dir_name.startswith("backup"):
        return True
    return any(
        tok in _BACKUP_DIR_TOKENS for tok in re.split(r"[.\-_~\s]+", lower_dir_name)
    )


#: Claimed ids probed per LanceDB filter query and fetched per SQLite
#: ``fetchmany`` (bounded memory on BOTH stores; batch-7/batch-9 reviews).
_ID_PROBE_BATCH = 500

_CLAIMED_IDS_SQL = (
    "SELECT id FROM files WHERE status IN ('indexed', 'partial') ORDER BY id"
)


def _iter_claimed_id_batches(sqlite_path: Path) -> Iterator[list[str]]:
    """Stream the claiming rows' ids in ``_ID_PROBE_BATCH``-sized batches.

    The cursor is read with ``fetchmany`` so the SQLite side is never
    materialised either (batch-9 review); the connection closes when the
    generator is exhausted or closed early.
    """
    conn = sqlite3.connect(str(sqlite_path), timeout=2.0, isolation_level=None)
    try:
        cursor = conn.execute(_CLAIMED_IDS_SQL)
        while True:
            rows = cursor.fetchmany(_ID_PROBE_BATCH)
            if not rows:
                return
            yield [str(row[0]) for row in rows]
    finally:
        conn.close()


def _index_shares_claimed_ids(
    lancedb_path: Path, id_batches: Iterable[list[str]]
) -> bool | None:
    """Whether ANY claimed file id has chunks in the LanceDB index, or None.

    None means "no identity evidence" (no chunks table, no ``file_id``
    column, an empty table or one whose ``file_id`` values are all NULL, or
    an index the installed lancedb cannot read)
    — the caller must then NOT refuse on identity grounds. A SQLite-layer
    failure of the id stream itself (lock, unreadable file, open failure)
    is NOT "no evidence": it raises ``MigrationRefused`` (exit 5 via
    ``main()``) so the destructive path cannot proceed on a skipped check
    (issue #1094 — the old lance-scoped ``except Exception`` swallowed these
    into None, which the caller treated as a pass). Neither store is
    materialised: ``id_batches`` arrive pre-bounded (streamed from SQLite),
    each is probed with a ``file_id IN (...)`` filter selecting at most one
    row, and the scan stops at the first batch that matches
    (pr1081-20261106 batch-7/batch-9 reviews). The batch iterator is always
    closed, so a streaming SQLite cursor is released on an early return.
    """
    try:
        if not (lancedb_path / "chunks.lance").exists():
            return None
        try:
            import lancedb as _ldb  # type: ignore

            db = _ldb.connect(str(lancedb_path))
            if "chunks" not in db.table_names():
                return None
            tbl = db.open_table("chunks")
            # An empty table, or one whose file_id values are all NULL,
            # carries no identity evidence (batch-7 delta review: all-NULL
            # must not read as a mismatch).
            if "file_id" not in tbl.schema.names or not tbl.count_rows(
                "file_id IS NOT NULL"
            ):
                return None
        except Exception as exc:
            logger.debug("Could not probe chunk file ids via lancedb: %s", exc)
            return None
        # The batch loop sits OUTSIDE the lance-side handler (issue #1094):
        # lance query failures keep their documented None, while a SQLite
        # error raised by the stream is a refusal.
        try:
            for batch in id_batches:
                if not batch:
                    continue
                literals = ", ".join("'" + i.replace("'", "''") + "'" for i in batch)
                try:
                    hit = (
                        tbl.search()
                        .where(f"file_id IN ({literals})")
                        .select(["file_id"])
                        .limit(1)
                        .to_arrow()
                    )
                except Exception as exc:
                    logger.debug(
                        "Could not probe chunk file ids via lancedb: %s", exc
                    )
                    return None
                if hit.num_rows:
                    return True
            return False
        except sqlite3.Error as exc:
            raise MigrationRefused(
                f"The claimed-id identity probe could not read the SQLite "
                f"database ({exc}).\n"
                "A failed identity check must not read as a pass, so the "
                "migration refuses before any reset or wipe. Stop the "
                "application (e.g. docker compose stop knowledgevault) or "
                "repair the database's permissions, then re-run."
            ) from exc
    finally:
        close = getattr(id_batches, "close", None)
        if close is not None:
            close()


# ---------------------------------------------------------------------------
# Migration steps
# ---------------------------------------------------------------------------

#: Collision suffix budget for rollback directory names (issue #1094): a
#: same-second re-run appends ``-1``..``-5`` before refusing.
_ROLLBACK_NAME_ATTEMPTS = 6


def _preserve_as_rollback(lancedb_path: Path) -> Path:
    """Rename the index directory to a timestamped rollback sibling and return it.

    The rollback name is printed by the caller BEFORE the live directory is
    recreated, so a recreation failure still leaves the recovery location
    operator-visible. Rename failure (locked handles, permissions) refuses
    with ``MigrationRefused`` — never falls back to deleting the index.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{lancedb_path.name}.rollback-{stamp}"
    for attempt in range(_ROLLBACK_NAME_ATTEMPTS):
        candidate = (
            lancedb_path.parent / base
            if attempt == 0
            else lancedb_path.parent / f"{base}-{attempt}"
        )
        if candidate.exists():
            continue
        try:
            lancedb_path.rename(candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise MigrationRefused(
                f"Could not preserve the LanceDB index as a rollback "
                f"directory ({exc}): {lancedb_path}\n"
                "The SQLite status reset has ALREADY committed, but the "
                "index itself is INTACT and untouched. Free the lock "
                "(stop any process holding the index files open) or fix "
                "the directory permissions, then re-run this script — the "
                "re-run is the documented interrupted-run repair."
            ) from exc
        return candidate
    raise MigrationRefused(
        f"Could not find a free rollback directory name for: {lancedb_path}\n"
        f"Remove stale {lancedb_path.name}.rollback-* directories (they are "
        "manual-delete only) and re-run. Nothing was wiped."
    )


def _wipe_lancedb(lancedb_path: Path, dry_run: bool) -> Path | None:
    """Replace the LanceDB index with an empty directory, preserving the
    previous index as a timestamped rollback sibling (issue #1094).

    Returns the rollback directory when one was preserved, else None. An
    empty index directory is left as-is: there is nothing to preserve and
    nothing to delete.
    """
    if not lancedb_path.exists():
        logger.info("LanceDB directory does not exist — nothing to wipe: %s", lancedb_path)
        return None

    if not any(lancedb_path.iterdir()):
        logger.info("LanceDB directory is empty — left as-is: %s", lancedb_path)
        return None

    if dry_run:
        logger.info(
            "[DRY RUN] Would preserve the LanceDB directory as a timestamped "
            "rollback sibling and recreate it empty: %s",
            lancedb_path,
        )
        return None

    rollback = _preserve_as_rollback(lancedb_path)
    # Printed BEFORE the recreation: if recreation fails, the recovery
    # location is already operator-visible.
    logger.info(
        "Previous LanceDB index preserved as rollback directory: %s "
        "(remove it manually once the rebuilt index is verified)",
        rollback,
    )
    try:
        lancedb_path.mkdir(parents=True)
    except OSError as exc:
        raise MigrationRefused(
            f"Could not recreate the empty LanceDB directory ({exc}): "
            f"{lancedb_path}\n"
            f"The previous index is PRESERVED at: {rollback}\n"
            "Fix the directory permissions and re-run this script — the "
            "re-run is the documented interrupted-run repair."
        ) from exc
    logger.info("Recreated empty LanceDB directory: %s", lancedb_path)
    return rollback


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


def _assert_application_database(sqlite_path: Path, lancedb_path: Path) -> None:
    """Refuse destructive steps without a readable application database (#749).

    A reset/wipe driven from a MISSING SQLite path (or a present file that is
    not the application database — no ``files`` table, an unrelated ``files``
    table without the application columns, or a backup copy of the database
    such as ``app.db.bak``) destroys the LanceDB index with no usable rollback
    source: the reset step would hit the wrong database and the wipe still
    follows it. Exit 8 refuses each of those states up front, naming both
    paths. Schema identity requires the reset contract's core ``files``
    columns (``id``/``status`` — the pivot of the whole reset/wipe flow),
    so an unrelated database that merely carries a table named ``files`` is
    refused too. A schema-valid database whose vector-claiming rows share no
    file id with a non-empty index's chunks is refused as describing a
    different index. Heuristics, not identity: a byte-identical copy of the
    live database under a live-looking name, and a foreign deployment's
    database whose autoincrement ids overlap the index's, are NOT
    detectable by content.

    Exception taxonomy deliberately mirrors the sibling probes
    (``_count_rows_claiming_vectors`` / ``_assert_no_active_writer``): a writer
    lock (``sqlite3.OperationalError`` is a SUBCLASS of ``sqlite3.DatabaseError``,
    so it must be caught first) stays a stop-the-writer ``MigrationRefused``
    (exit 5 via ``main()``); other ``OperationalError`` (permission/read-only
    I/O failures) gets its own open-failure diagnostic instead of the lock
    advice; other ``DatabaseError`` is corruption, also exit 5.
    """
    if not sqlite_path.exists():
        print(
            "ERROR: the application SQLite database does not exist, so a\n"
            "reset/wipe would destroy the LanceDB index with NO rollback\n"
            "source (the status reset would skip and the wipe still runs):\n"
            f"  SQLite path : {sqlite_path} (missing)\n"
            f"  LanceDB path: {lancedb_path}\n"
            "Point --sqlite-path at the application database (as seen from\n"
            "where you run the script) and re-run. Nothing was migrated.\n"
        )
        raise SystemExit(8)
    _lower_name = sqlite_path.name.lower()
    # Symlink resolution (pr1081-20261106 batch-2): a live-looking name can
    # point AT a backup, so the refusal checks the RESOLVED target's name too.
    try:
        _resolved = sqlite_path.resolve()
    except OSError:  # pragma: no cover - unresolvable path cannot be opened below
        _resolved = sqlite_path
    _resolved_name = _resolved.name.lower()
    if (
        _looks_like_backup(_lower_name)
        or _looks_like_backup(_resolved_name)
        or _dir_looks_like_backup(sqlite_path.parent.name.lower())
        or _dir_looks_like_backup(_resolved.parent.name.lower())
    ):
        print(
            "ERROR: the SQLite path looks like a BACKUP copy, not the live\n"
            "application database:\n"
            f"  SQLite path : {sqlite_path}\n"
            + (
                f"  resolves to : {_resolved}\n"
                if _resolved != sqlite_path
                else ""
            )
            + f"  LanceDB path: {lancedb_path}\n"
            "Migrating against a backup resets the COPY's rows while the live\n"
            "LanceDB index is still wiped — the live database keeps recording\n"
            "'indexed' and the corpus becomes silently unsearchable. Point\n"
            "--sqlite-path at the live application database and re-run.\n"
            "Nothing was migrated.\n"
        )
        raise SystemExit(8)
    conn = None
    try:
        conn = sqlite3.connect(str(sqlite_path), timeout=2.0, isolation_level=None)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        )
        has_files_table = cursor.fetchone() is not None
    except sqlite3.OperationalError as exc:
        if "lock" in str(exc).lower() or "busy" in str(exc).lower():
            raise MigrationRefused(
                f"Another process appears to hold a write lock on the SQLite "
                f"database ({exc}): {sqlite_path}\n"
                "Is the application still running? Stop it first, e.g.:\n"
                "  docker compose stop knowledgevault\n"
                "Then re-run this migration."
            ) from exc
        raise MigrationRefused(
            f"The SQLite database could not be opened ({exc}): {sqlite_path}\n"
            "This is not a writer lock — check the file's permission and\n"
            "ownership (read-only mount, unreadable file) before re-running."
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise MigrationRefused(
            f"The SQLite database is unreadable or corrupt ({exc}): "
            f"{sqlite_path}\n"
            "Repair or restore it (from your backup) before re-running."
        ) from exc
    finally:
        if conn is not None:
            conn.close()
    if not has_files_table:
        print(
            "ERROR: the SQLite file exists but has no 'files' table, so it is\n"
            "not the application database — a reset/wipe would destroy the\n"
            "LanceDB index with NO rollback source:\n"
            f"  SQLite path : {sqlite_path} (no 'files' table)\n"
            f"  LanceDB path: {lancedb_path}\n"
            "Point --sqlite-path at the application database and re-run.\n"
            "Nothing was migrated.\n"
        )
        raise SystemExit(8)
    conn = None
    try:
        # Inside the taxonomy like the first connection (batch-2 review): a
        # failure here must surface as MigrationRefused guidance, not a
        # bare traceback.
        conn = sqlite3.connect(str(sqlite_path), timeout=2.0, isolation_level=None)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(files)").fetchall()
        }
        claimed_count = 0
        if {"id", "status"}.issubset(columns):
            # A COUNT, not the id list: the ids themselves are streamed in
            # bounded batches only if the cross-store probe runs (batch-9).
            claimed_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM files WHERE status IN ('indexed', 'partial')"
                ).fetchone()[0]
            )
    except sqlite3.OperationalError as exc:
        if "lock" in str(exc).lower() or "busy" in str(exc).lower():
            raise MigrationRefused(
                f"Another process appears to hold a write lock on the SQLite "
                f"database ({exc}): {sqlite_path}\n"
                "Is the application still running? Stop it first, e.g.:\n"
                "  docker compose stop knowledgevault\n"
                "Then re-run this migration."
            ) from exc
        raise MigrationRefused(
            f"The SQLite database could not be opened ({exc}): {sqlite_path}\n"
            "This is not a writer lock — check the file's permission and\n"
            "ownership (read-only mount, unreadable file) before re-running."
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise MigrationRefused(
            f"The SQLite database is unreadable or corrupt ({exc}): "
            f"{sqlite_path}\n"
            "Repair or restore it (from your backup) before re-running."
        ) from exc
    finally:
        if conn is not None:
            conn.close()
    if not {"id", "status"}.issubset(columns):
        print(
            "ERROR: the SQLite file has a 'files' table without the\n"
            "application's core columns (id, status), so it is not\n"
            "the application database — a reset/wipe would destroy the\n"
            "LanceDB index with NO rollback source:\n"
            f"  SQLite path : {sqlite_path} (files columns: "
            f"{sorted(columns) or 'none'})\n"
            f"  LanceDB path: {lancedb_path}\n"
            "Point --sqlite-path at the application database (start the\n"
            "application once so its migrations create the schema) and\n"
            "re-run. Nothing was migrated.\n"
        )
        raise SystemExit(8)
    # Cross-store identity (pr1081-20261106 batch-5 review): rows that claim
    # vectors ('indexed'/'partial') must have chunks in THIS index. When the
    # index's chunks carry file ids and NONE of the claimed rows appear among
    # them, the database does not describe this index (unrelated or foreign
    # deployment's database) — the reset would hit the wrong rows while the
    # live index is wiped. Zero claims (fresh boot, reset-then-crash repair
    # re-run) and an index without readable file ids carry no evidence:
    # the run proceeds (zero claims still get run_migration's WARNING). The
    # check proves only DISJOINTNESS — a byte-identical live copy, or a
    # foreign deployment's database whose autoincrement ids overlap the
    # index's, passes; the backup-name check above is the only other guard.
    if claimed_count:
        shares = _index_shares_claimed_ids(
            lancedb_path, _iter_claimed_id_batches(sqlite_path)
        )
        if shares is False:
            print(
                "ERROR: none of the database's rows that claim vectors\n"
                "('indexed'/'partial') have chunks in this LanceDB index, so\n"
                "the database does not describe the index about to be wiped:\n"
                f"  SQLite path : {sqlite_path} "
                f"({claimed_count} claiming row(s))\n"
                f"  LanceDB path: {lancedb_path} (non-empty, none shared)\n"
                "Point --sqlite-path at the database that produced this index\n"
                "(and --lancedb-path at its index) and re-run. Nothing was\n"
                "migrated.\n"
            )
            raise SystemExit(8)


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
        # per the schema that actually exists on this database. EVERY column
        # beyond ``status`` is guarded (batch-2: minimal-schema application
        # databases reset cleanly — the old unconditional ``chunk_count = 0``
        # broke them).
        columns = _table_columns(conn, "files")
        set_fragments = [
            "status = 'pending'",
        ]
        if "chunk_count" in columns:
            set_fragments.append("chunk_count = 0")
        if "processed_at" in columns:
            set_fragments.append("processed_at = NULL")
        if "modified_at" in columns:
            set_fragments.append("modified_at = CURRENT_TIMESTAMP")
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
            corrupt during repair detection; code 7 when neither path
            exists; code 8 when the application database is unusable for a
            reset/wipe (dry runs included): missing, no ``files`` table, a
            ``files`` table without the application's core columns, an
            obvious backup copy, or vector-claiming rows that share no file
            id with a non-empty index's chunks. These are heuristics,
            not identity: a byte-identical copy of the live database, or a
            foreign deployment's database whose autoincrement ids overlap
            the index's, passes them. A database with no
            vector-claiming rows
            gets a loud wrong-database WARNING on the destructive path and
            dry-run.
        MigrationRefused: another process holds a write lock on the SQLite
            database, the database is unreadable/corrupt at the writer
            probe, the claimed-id identity probe could not read the SQLite
            database (a probe failure is a refusal, never a pass — issue
            #1094), or the rollback-preserving index replacement itself
            failed (rename/recreation; the message names the recovery
            state) (``main()`` maps this to exit code 5).
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

    # Issue #749 (T1-29-K-02): refusing BOTH-missing paths is the first check.
    # A run pointed at a LanceDB path AND a SQLite path that both do not exist
    # is almost always wrong paths (for example container-side DATA_DIR paths
    # run from the host), and the old behavior — a silent "Nothing to migrate"
    # exit 0 — told the operator the migration had run. Applies to --dry-run
    # and --force alike: wrong paths are wrong paths (7 joins 5/6 in the
    # always-stop family; --force overrides 3/4 only).
    if not lancedb_path.exists() and not sqlite_path.exists():
        print(
            "ERROR: neither path exists — refusing to continue:\n"
            f"  LanceDB path: {lancedb_path} (exists: {lancedb_path.exists()})\n"
            f"  SQLite path : {sqlite_path} (exists: {sqlite_path.exists()})\n"
            "This usually means the paths are wrong (for example container-side\n"
            "DATA_DIR paths used from the host). Pass the paths as seen from\n"
            "where you run the script. Nothing was migrated.\n"
        )
        raise SystemExit(7)

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
            # `_wipe_lancedb` leaves behind (an empty recreated directory). An ABSENT
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
            "WARNING: This will replace the LanceDB index.\n"
            "         The previous index is preserved as a timestamped rollback\n"
            "         directory next to it (see the run summary); rebuilding\n"
            "         temporarily needs space for BOTH indexes.\n"
        )
        # Issue #749: the destructive path requires the application database —
        # without it the reset skips and the wipe still runs (no rollback
        # source). Checked BEFORE the writer probe so a missing/empty DB is
        # refused first; locks and corruption keep the writer probe's
        # exit-5 semantics wherever the database exists.
        _assert_application_database(sqlite_path, lancedb_path)
        # Issue #749 (batch-2 review): warn loudly when THIS database holds
        # no rows claiming vectors (indexed/partial) while a non-empty
        # LanceDB index is about to be wiped — that is the wrong-database
        # shape the executed exploit used. It is a WARNING, not a refusal:
        # the identical state is also the script's own documented
        # reset-then-crash repair re-run (the reset commits before the wipe),
        # and a fresh boot creates an empty chunks table before any ingest,
        # so the script cannot distinguish wrong-database from repair and a
        # hard refusal broke both contracts (delta-review executed probe).
        if lancedb_path.exists() and any(lancedb_path.iterdir()):
            claims = _count_rows_claiming_vectors(sqlite_path)
            if claims == 0:
                print(
                    "WARNING: the application database holds NO rows claiming\n"
                    "vectors ('indexed'/'partial'), so nothing in it accounts\n"
                    "for the LanceDB index about to be wiped:\n"
                    f"  {lancedb_path}\n"
                    "If this database is not the live application database\n"
                    "(backup copy, fresh database, wrong deployment), STOP —\n"
                    "wiping leaves a corpus nothing in it re-indexes. If this\n"
                    "IS the re-run after an interrupted migration (reset done,\n"
                    "wipe pending), proceeding is the documented repair.\n"
                )
        # Issue #694: refuse to race a live writer. Probe BEFORE the first
        # destructive step (the SQLite reset — the wipe follows it).
        _assert_no_active_writer(sqlite_path)
    else:
        # Dry-run reports what WOULD change; over wrong paths that report is
        # the misleading success the refusals exist for (issue #749).
        _assert_application_database(sqlite_path, lancedb_path)
        if lancedb_path.exists() and any(lancedb_path.iterdir()):
            if _count_rows_claiming_vectors(sqlite_path) == 0:
                print(
                    "WARNING: the application database holds NO rows claiming\n"
                    "vectors ('indexed'/'partial') for the LanceDB index that\n"
                    "would be wiped. If this is not the live application\n"
                    "database, STOP — wiping leaves a corpus nothing in it\n"
                    "re-indexes. If this IS the re-run after an interrupted\n"
                    "migration (reset done, wipe pending), proceeding is the\n"
                    "documented repair.\n"
                )

    # ── Step 2: Reset file statuses ────────────────────────────────────────
    # The reset runs FIRST (issue #694): if it fails (locked DB, crash), the
    # LanceDB index is still intact as the rollback source and the rows keep
    # claiming vectors they still have. A crash after the reset but before
    # the wipe is repaired by re-running the script.
    print("[1/2] Resetting file statuses to 'pending'...")
    reset_count = _reset_file_statuses(sqlite_path, dry_run=dry_run)

    # ── Step 3: Replace the LanceDB index ──────────────────────────────────
    print("[2/2] Replacing LanceDB vector index (previous index preserved)...")
    rollback_dir = _wipe_lancedb(lancedb_path, dry_run=dry_run)

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    if dry_run:
        print(
            f"[DRY RUN] Migration summary:\n"
            f"  LanceDB replacement  : {'yes' if lancedb_path.exists() else 'no (already empty)'}\n"
            f"  Files to reset       : {reset_count}\n"
            f"\nRe-run without --dry-run to apply changes."
        )
    else:
        print(
            f"Migration complete:\n"
            f"  Files reset          : {reset_count}\n"
            f"  LanceDB replaced     : yes\n"
            + (
                f"  Previous index at    : {rollback_dir}\n"
                if rollback_dir is not None
                else ""
            )
            + "\nNext step: start the application to begin re-indexing.\n"
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
