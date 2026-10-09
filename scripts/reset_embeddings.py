"""Clear embeddings and reset file statuses for model migration.

Stop the application first (e.g. ``docker compose stop knowledgevault``):
without the app quiesced, a live writer can race this script's LanceDB wipe
and the SQLite status reset.

Issue #705: the SQLite status reset runs BEFORE the LanceDB wipe (the same
ordering scripts/migrate_embeddings.py adopted for issue #694). The wipe is
the only irreversible step, so a failed or interrupted SQLite reset leaves
the old vectors intact as the rollback source instead of stranding rows that
claim vectors over an empty index.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from backend.app.config import settings
from backend.app.models.database import get_db_connection

RESET_TABLE_STATUSES = ("indexed", "partial", "pending")


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    """Column names of the ``files`` table (empty when the table is absent)."""
    # nosec B608 - static table literal, never user input
    cursor = conn.execute("PRAGMA table_info(files)")
    return {row[1] for row in cursor.fetchall()}


def reset_all_embeddings():
    """Delete all embeddings and reset file statuses."""

    print("=== Migration: Clearing Embeddings ===")
    print(f"SQLite DB: {settings.sqlite_path}")
    print(f"LanceDB path: {settings.lancedb_path}")
    print("WARNING: stop the application before running this script.")
    print()

    # 1. Reset file statuses in SQLite FIRST (issue #705): if this step fails
    # (locked/corrupt database), the exception propagates unchanged and the
    # LanceDB wipe below never runs — the old vectors survive as the rollback
    # source instead of being destroyed over rows that still claim them.
    print("[1/3] Resetting file statuses...")
    conn = get_db_connection(str(settings.sqlite_path))
    try:
        set_fragments = [
            "status = 'pending'",
            "chunk_count = 0",
            "processed_at = NULL",
            "modified_at = CURRENT_TIMESTAMP",
        ]
        columns = _table_columns(conn)
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

    # 2. Clear LanceDB by deleting the entire database directory — only after
    # the status reset succeeded.
    print("[2/3] Clearing LanceDB database...")
    lancedb_path = settings.lancedb_path

    if lancedb_path.exists():
        shutil.rmtree(lancedb_path)
        lancedb_path.mkdir(parents=True, exist_ok=True)
        print(f"      Deleted LanceDB directory and recreated")
    else:
        print(f"      LanceDB directory does not exist - nothing to clear")

    print("[3/3] Migration complete")
    print()
    print("Next steps:")
    print("1. Update EMBEDDING_MODEL in .env or docker-compose.yml")
    print("   Recommended: qwen3-embed:4b (text embedding)")
    print("   Note: qwen3-vl-embedding-2b is vision-language, not ideal for text RAG")
    print("2. Restart backend service:")
    print("   docker compose restart backend")
    print(
        f"3. Background processor will auto-reprocess the {updated_count} reset file(s)"
    )
    print()


if __name__ == "__main__":
    reset_all_embeddings()
