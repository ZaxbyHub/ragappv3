"""Persistent, disk-backed embedding cache (issue #513 W24).

The cache is keyed by the IMMUTABLE embedding contract — (model id, model
revision, doc prefix, dim, normalized text) — so any contract change produces
different cache keys and invalidates by construction; there is no TTL and no
manual invalidation path to forget. Byte-identical normalized text under the
same contract yields a hit across restarts (the store is sqlite on disk).

Storage decision: this module owns a DEDICATED sqlite file
(``<data_dir>/embedding_cache.db``) with its own lazily-created connection.
The table is intentionally NOT declared in the main application schema
(``app.models.database``) — nothing would ever read or write it through the
main DB, so a main-DB declaration would be unused dead schema (repo
non-negotiable: never ship unwired code). All schema lives here, beside its
only consumer.

Failure policy: the cache is strictly best-effort. ``lookup`` treats corrupt/
undecodable rows as misses (deleting them) and returns ``{}`` on any sqlite
error; ``store`` swallows and logs sqlite errors. Failures NEVER fail the
embedding path — a cache miss just means the provider is called.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from pathlib import Path

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# Separator for the key pre-image. A control character cannot occur in model
# ids, revisions, prefixes, or numeric dims, and normalized text is delimited
# last, so component boundaries are unambiguous (no collision via
# concatenation).
_KEY_SEPARATOR = "\x1f"

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()


def _cache_db_path() -> Path:
    return Path(settings.data_dir) / "embedding_cache.db"


def _get_conn() -> sqlite3.Connection:
    """Lazily open (and schema-ensure) the cache's own sqlite connection.

    ``check_same_thread=False`` plus the module lock make the single shared
    connection safe for use from ingestion workers and route handlers alike.

    Lock discipline: this function acquires ``_conn_lock`` internally — callers
    must NOT already hold it (the lock is not reentrant; ``lookup``/``store``
    serialize their operations on it AFTER calling this).
    """
    global _conn
    with _conn_lock:
        if _conn is None:
            db_path = _cache_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    cache_key TEXT PRIMARY KEY,
                    vector BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            _conn = conn
        return _conn


def embedding_cache_key(
    model_id: str,
    model_revision: str,
    doc_prefix: str,
    dim: int,
    normalized_text: str,
) -> str:
    """Deterministic cache key binding the full immutable embedding contract.

    sha256 hex of the components joined with ``\\x1f`` (a separator that
    cannot occur inside any component, so joins are collision-free). Changing
    ANY component — model id, revision, doc prefix, dim, or the normalized
    text — changes the key, invalidating the old entry by construction.
    """
    preimage = _KEY_SEPARATOR.join(
        (model_id, model_revision, doc_prefix, str(int(dim)), normalized_text)
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def lookup(keys: list[str]) -> dict[str, list[float]]:
    """Best-effort bulk lookup: returns the subset of keys that hit.

    Corrupt/undecodable rows (wrong byte length for ``dim``, or a non-float32
    payload) are treated as misses and deleted. Any sqlite error logs a
    warning and yields ``{}`` — the caller simply re-embeds.
    """
    if not keys:
        return {}
    found: dict[str, list[float]] = {}
    try:
        conn = _get_conn()
        with _conn_lock:
            placeholders = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT cache_key, vector, dim FROM embedding_cache "
                f"WHERE cache_key IN ({placeholders})",  # nosec B608 — placeholders is a fixed '?,?,...' literal, keys are bound
                keys,
            ).fetchall()
            for cache_key, blob, dim in rows:
                if not isinstance(blob, (bytes, bytearray)):
                    conn.execute(
                        "DELETE FROM embedding_cache WHERE cache_key = ?",
                        (cache_key,),
                    )
                    continue
                expected_len = int(dim) * 4
                if expected_len <= 0 or len(blob) != expected_len:
                    # Corrupt row (dim/blob mismatch) — miss + prune.
                    conn.execute(
                        "DELETE FROM embedding_cache WHERE cache_key = ?",
                        (cache_key,),
                    )
                    continue
                try:
                    vector = np.frombuffer(blob, dtype=np.float32).tolist()
                except (ValueError, TypeError):
                    conn.execute(
                        "DELETE FROM embedding_cache WHERE cache_key = ?",
                        (cache_key,),
                    )
                    continue
                found[cache_key] = vector
            conn.commit()
    except Exception:
        logger.warning("embedding cache lookup failed (treating as all-miss)", exc_info=True)
        return {}
    return found


def store(items: list[tuple[str, list[float]]]) -> None:
    """Best-effort bulk store (INSERT OR REPLACE), then prune to the cap.

    After writing, deletes the oldest rows (by ``created_at``, ``rowid``
    tie-break) beyond ``settings.embedding_cache_max_entries`` with a single
    DELETE + subquery. Re-storing an existing key refreshes its
    ``created_at`` (INSERT OR REPLACE re-inserts the row), so pruning is
    recency-based. Failures are logged and never propagate.
    """
    if not items:
        return
    try:
        rows = []
        for cache_key, vector in items:
            arr = np.asarray(vector, dtype=np.float32)
            rows.append((cache_key, arr.tobytes(), int(arr.shape[0])))
        conn = _get_conn()
        with _conn_lock:
            conn.executemany(
                "INSERT OR REPLACE INTO embedding_cache (cache_key, vector, dim) "
                "VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
            cap = int(getattr(settings, "embedding_cache_max_entries", 50000))
            excess = int(total) - cap
            if excess > 0:
                conn.execute(
                    "DELETE FROM embedding_cache WHERE cache_key IN ("
                    "SELECT cache_key FROM embedding_cache "
                    "ORDER BY created_at ASC, rowid ASC LIMIT ?)",
                    (excess,),
                )
                conn.commit()
    except Exception:
        logger.warning(
            "embedding cache store failed (cache disabled for this batch)",
            exc_info=True,
        )
