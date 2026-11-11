"""Clear embeddings and reset file statuses for model migration.

Stop the application first (e.g. ``docker compose stop knowledgevault``):
without the app quiesced, a live writer can race this script's SQLite status
reset and the LanceDB replacement.

Issue #705: the SQLite status reset runs BEFORE the LanceDB replacement (the
same ordering scripts/migrate_embeddings.py adopted for issue #694). The
replacement preserves the previous index as a timestamped rollback sibling
(``<name>.rollback-YYYYMMDD-HHMMSS``) and recreates the live directory empty
(issue #1080) — the destructive step no longer deletes vectors irreversibly,
and a schema-compatible but WRONG database at worst leaves a recoverable
rollback directory instead of destroying the live index. Rollback
directories accumulate next to the live index until you remove them
(``scripts/cleanup_backups.py`` does NOT manage them); rebuilding temporarily
needs space for BOTH indexes. A rollback directory holds the previous
index's full contents (including all document chunk text), is NOT covered by
in-app document/vault deletion, and is removed only by manual deletion once
the rebuilt index is verified. If the rename or the recreation fails, the
script refuses: the SQLite reset has already committed at that point (the
reset rows are re-adopted by the successful re-run), and the refusal names
the recovery state.

Issue #1080: the script REFUSES (exit code 8, mirroring the migrate guard's
database-shape taxonomy) before any reset, wipe, or database-file creation
when the configured database cannot be the application database: the file is
missing, the path is a directory, the path looks like a backup copy (the
same name/parent tokens migrate refuses), the database cannot be OPENED
(lock-shaped or permission failures → stop-the-app / check-permissions
advice — a live writer that holds the database surfaces later at the reset
step as a locked error, with the index untouched), corrupt, it has no
``files`` table, or the ``files`` table lacks the application's core columns
(id, status). The reset does NOT run migrate's cross-store identity probe
(migrate's ``--sqlite-path`` flag makes that probe meaningful there; reset's
paths derive from DATA_DIR). A symlinked/junctioned index path or an
index directory that cannot be inspected is refused BEFORE the reset
commits (nothing is reset or renamed). Exit 8 is dual-use: after the
reset has committed, a rollback preservation failure (rename/recreate)
also exits 8 and names the rollback copy.
"""

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))
# backend.app.config imports ``app.*`` absolutely (config.py L13), which
# only resolves with backend/ itself on the path — the documented
# repo-root invocation has no PYTHONPATH (issue #727 / T1-28-K-01).
sys.path.append(str(ROOT / "backend"))

from backend.app.config import settings
from backend.app.models.database import get_db_connection

RESET_TABLE_STATUSES = ("indexed", "partial", "pending")

#: Collision suffix budget for rollback directory names (issue #1080): a
#: same-second re-run appends ``-1``..``-5`` before refusing.
_ROLLBACK_NAME_ATTEMPTS = 6

#: Name tokens that mark a backup copy (the same sets migrate refuses;
#: split on . - _ ~ and whitespace).
_BACKUP_TOKENS = frozenset({"bak", "backup", "orig", "copy"})

#: Parent-directory tokens that mark a backup location.
_BACKUP_DIR_TOKENS = frozenset({"backup", "backups"})


def _looks_like_backup(lower_name: str) -> bool:
    """Whether a lower-cased file name looks like a backup copy (token-based
    so rotated and stamped forms are caught too)."""
    if lower_name.startswith(("backup", "copy of")) or lower_name.endswith("~"):
        return True
    return any(tok in _BACKUP_TOKENS for tok in re.split(r"[.\-_~\s]+", lower_name))


def _dir_looks_like_backup(lower_dir_name: str) -> bool:
    """Whether the database's IMMEDIATE parent directory is a backup
    location (``backups/app.db``; no ``bak``/``orig``/``copy`` tokens)."""
    if lower_dir_name.startswith("backup"):
        return True
    return any(
        tok in _BACKUP_DIR_TOKENS for tok in re.split(r"[.\-_~\s]+", lower_dir_name)
    )


def _is_link_or_junction(path: Path) -> bool:
    """A symlink OR a Windows junction/reparse point (``is_symlink()`` alone
    is false for junctions; the same probe scripts/restore.py uses)."""
    if path.is_symlink():
        return True
    try:
        os.readlink(path)
        return True
    except (OSError, ValueError):
        return False


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    """Column names of the ``files`` table (empty when the table is absent)."""
    cursor = conn.execute("PRAGMA table_info(files)")
    return {row[1] for row in cursor.fetchall()}


def _assert_application_database(sqlite_path: Path, lancedb_path: Path) -> None:
    """Refuse (exit 8) unless the configured database can be the application
    database — the database-shape taxonomy mirrors
    ``scripts/migrate_embeddings.py`` (issue #1080; reset does not run
    migrate's cross-store identity probe).

    Runs BEFORE ``get_db_connection`` so a missing or unusable path is
    refused without materializing a stray database file, and before any
    wipe so the index always survives the refusal.
    """

    def _refuse(detail: str, remedy: str) -> None:
        print(
            f"ERROR: {detail}\n"
            f"  SQLite path : {sqlite_path}\n"
            f"  LanceDB path: {lancedb_path}\n"
            f"{remedy}\n"
            "Nothing was reset and the LanceDB index was not touched.\n"
        )
        raise SystemExit(8)

    if sqlite_path.is_dir():
        _refuse(
            "the configured SQLite path is a DIRECTORY, not the application "
            "database file",
            "Point the SQLite path (DATA_DIR / settings.sqlite_path) at the "
            "application database file.",
        )
    if not sqlite_path.exists():
        _refuse(
            "the configured SQLite database file does not exist, so this "
            "cannot be the application database — a reset would run "
            "against a database that does not describe this index while "
            "the index is replaced (the previous index survives only as a "
            "rollback copy)",
            "Start the application once so it creates the database (or fix "
            "DATA_DIR / the SQLite path), then re-run.",
        )
    resolved = sqlite_path
    try:
        resolved = sqlite_path.resolve()
    except OSError:  # pragma: no cover - unresolvable path handled below
        pass
    if (
        _looks_like_backup(sqlite_path.name.lower())
        or _looks_like_backup(resolved.name.lower())
        or _dir_looks_like_backup(sqlite_path.parent.name.lower())
        or _dir_looks_like_backup(resolved.parent.name.lower())
    ):
        _refuse(
            "the SQLite path looks like a BACKUP copy, not the live "
            "application database",
            "Resetting against a backup rewrites the COPY's rows while the "
            "live index is replaced — the live database keeps recording "
            "'indexed' and the corpus becomes silently unsearchable. Point "
            "DATA_DIR / the SQLite path at the live application database "
            "and re-run.",
        )
    conn = None
    try:
        conn = sqlite3.connect(str(sqlite_path), timeout=2.0)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    except sqlite3.OperationalError as exc:
        if "lock" in str(exc).lower() or "busy" in str(exc).lower():
            _refuse(
                f"another process appears to hold a write lock on the SQLite "
                f"database ({exc})",
                "Is the application still running? Stop it first, e.g.:\n"
                "  docker compose stop knowledgevault\n"
                "Then re-run this script.",
            )
        _refuse(
            f"the SQLite database could not be opened ({exc})",
            "This is not a writer lock — check the file's permission and\n"
            "ownership (read-only mount, unreadable file) before re-running.",
        )
    except sqlite3.DatabaseError as exc:
        _refuse(
            f"the SQLite database is unreadable or corrupt ({exc})",
            "Repair or restore it (from your backup) before re-running.",
        )
    finally:
        if conn is not None:
            conn.close()
    if not columns:
        _refuse(
            "the SQLite file exists but has no 'files' table, so it is not "
            "the application database",
            "Point the SQLite path at the application database (start the\n"
            "application once so its migrations create the schema) and re-run.",
        )
    if not {"id", "status"}.issubset(columns):
        _refuse(
            "the SQLite file has a 'files' table without the application's "
            f"core columns (id, status) — files columns: {sorted(columns)}",
            "Point the SQLite path at the application database and re-run.",
        )


def _preserve_as_rollback(lancedb_path: Path) -> Path:
    """Rename the index directory to a timestamped rollback sibling and
    return it (the local twin of ``scripts/migrate_embeddings.py``'s helper,
    issue #1080: keep the operator scripts standalone — no cross-script
    import). Rename failure refuses instead of deleting the index; the
    symlink/junction refusal itself runs earlier, before the reset (see
    ``reset_all_embeddings``)."""

    def _refuse_state_aware(detail: str) -> None:
        print(
            f"ERROR: {detail}\n"
            f"  LanceDB path: {lancedb_path}\n"
            "The SQLite status reset has ALREADY committed (the reset rows\n"
            "are re-adopted by the successful re-run), but the index itself\n"
            "is INTACT and untouched. Close any program using these files or\n"
            "fix the directory permissions, then re-run this script — the\n"
            "re-run retries the replacement (stop the application first; the\n"
            "script does not probe the index for open handles).\n"
        )
        raise SystemExit(8)

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
            _refuse_state_aware(
                f"could not preserve the LanceDB index as a rollback "
                f"directory ({exc})"
            )
        return candidate
    _refuse_state_aware(
        f"could not find a free rollback directory name (remove stale "
        f"{lancedb_path.name}.rollback-* directories and re-run)"
    )
    raise AssertionError("unreachable")  # pragma: no cover - _refuse raises


def reset_all_embeddings():
    """Delete all embeddings and reset file statuses."""

    print("=== Migration: Clearing Embeddings ===")
    print(f"SQLite DB: {settings.sqlite_path}")
    print(f"LanceDB path: {settings.lancedb_path}")
    print("WARNING: stop the application before running this script.")
    print()

    # Issue #1080: refuse unusable databases BEFORE opening them (a bare
    # sqlite3.connect CREATES a missing file) and before any wipe.
    _assert_application_database(
        Path(str(settings.sqlite_path)), Path(str(settings.lancedb_path))
    )
    lancedb_path = Path(str(settings.lancedb_path))
    if lancedb_path.exists() and _is_link_or_junction(lancedb_path):
        print(
            f"ERROR: the LanceDB path is a symlink or junction, so "
            f"preserving it as a rollback directory would move the LINK "
            f"instead of the index it points at: {lancedb_path}\n"
            "Point DATA_DIR at a real directory (reconfigure the link target "
            "as the data location) and re-run. Nothing was reset or "
            "renamed.\n"
        )
        raise SystemExit(8)

    # 1. Reset file statuses in SQLite FIRST (issue #705): if this step fails
    # (locked/corrupt database), the exception propagates unchanged and the
    # LanceDB replacement below never runs — the old vectors survive as the
    # rollback source instead of being destroyed over rows that still claim
    # them.
    print("[1/3] Resetting file statuses...")
    conn = get_db_connection(str(settings.sqlite_path))
    try:
        # Only ``status`` is unconditional: the preflight guarantees it
        # exists, and a minimal-schema application database (id + status)
        # must reset cleanly instead of crashing mid-UPDATE (issue #1080,
        # mirroring migrate_embeddings._reset_file_statuses' batch-2 guards).
        set_fragments = [
            "status = 'pending'",
        ]
        columns = _table_columns(conn)
        if "chunk_count" in columns:
            set_fragments.append("chunk_count = 0")
        if "processed_at" in columns:
            set_fragments.append("processed_at = NULL")
        if "modified_at" in columns:
            set_fragments.append("modified_at = CURRENT_TIMESTAMP")
        if "phase" in columns:
            # Restore the queue signal the startup recovery sweep adopts
            # (background_tasks.py selects status='pending' AND phase='queued').
            set_fragments.append("phase = 'queued'")
        if "partial_embeddings" in columns:
            set_fragments.append("partial_embeddings = 0")
        if "chunks_failed" in columns:
            set_fragments.append("chunks_failed = 0")
        if "error_message" in columns:
            set_fragments.append("error_message = NULL")

        placeholders = ", ".join("?" for _ in RESET_TABLE_STATUSES)
        # nosec B608 - the SET fragments and WHERE statuses are literal SQL /
        # bound parameters; no user input is interpolated.
        cursor = conn.execute(
            "UPDATE files SET "
            + ", ".join(set_fragments)
            + f" WHERE status IN ({placeholders})",
            RESET_TABLE_STATUSES,
        )
        updated_count = cursor.rowcount
        conn.commit()
        print(
            f"      Reset {updated_count} file(s) from "
            "'indexed'/'partial'/'pending' to 'pending' (phase='queued')"
        )
    finally:
        conn.close()

    # 2. Replace LanceDB by preserving the previous index as a timestamped
    # rollback sibling and recreating the live directory empty — only after
    # the status reset succeeded (issues #705, #1080).
    print("[2/3] Replacing LanceDB database (previous index preserved)...")

    if lancedb_path.exists():
        try:
            has_data = any(lancedb_path.iterdir())
        except OSError as exc:
            print(
                f"ERROR: could not inspect the LanceDB directory to check "
                f"whether it still holds data ({exc}): {lancedb_path}\n"
                "The SQLite status reset has ALREADY committed, but the "
                "index itself is INTACT and untouched. Fix the directory "
                "permissions, then re-run this script.\n"
            )
            raise SystemExit(8)
        if has_data:
            rollback = _preserve_as_rollback(lancedb_path)
            # Printed BEFORE the recreation: if recreation fails, the
            # recovery location is already operator-visible.
            print(f"      Preserved previous index as rollback directory: {rollback}")
            try:
                lancedb_path.mkdir(parents=True)
            except OSError as exc:
                print(
                    f"ERROR: could not recreate the empty LanceDB directory "
                    f"({exc}): {lancedb_path}\n"
                    "The SQLite status reset has ALREADY committed and the "
                    "live index directory is now ABSENT. The previous index "
                    f"is PRESERVED at: {rollback}\n"
                    "To recover, do ONE of the following BEFORE re-running "
                    "this script:\n"
                    "  - recreate the directory manually, or\n"
                    f"  - rename the rollback directory back to "
                    f"{lancedb_path.name}.\n"
                    "A plain re-run WITHOUT restoring the directory reports "
                    "'LanceDB directory does not exist - nothing to clear' "
                    "and does not restore it.\n"
                )
                raise SystemExit(8)
            print(f"      Recreated empty LanceDB directory")
        else:
            print(f"      LanceDB directory is empty - left as-is")
    else:
        print(f"      LanceDB directory does not exist - nothing to clear")

    print("[3/3] Migration complete")
    print()
    print("Next steps:")
    print("1. Update EMBEDDING_MODEL in .env or docker-compose.yml")
    print("   Recommended: qwen3-embed:4b (text embedding)")
    print("   Note: qwen3-vl-embedding-2b is vision-language, not ideal for text RAG")
    print("2. Restart backend service:")
    print("   docker compose restart knowledgevault")
    print(
        f"3. Background processor will auto-reprocess the {updated_count} reset file(s)"
    )
    print(
        "   (a file whose LATEST ingestion job is still capped-failed keeps"
        " the terminal 'error' state — retry it per-file via the admin retry"
        " endpoint; older capped failures that a later job completed no"
        " longer block reset recovery. Files already settled to 'error' by"
        " an OLDER build stay in 'error' — this script resets only"
        " indexed/partial/pending rows, so recover those via the per-file"
        " admin retry endpoint too.)"
    )
    print()


if __name__ == "__main__":
    reset_all_embeddings()
