"""Advisory near-duplicate grouping for ingested documents (issue #513 W26).

ADVISORY ONLY — grouping never blocks, deletes, or rejects documents; distinct
revisions are always retained. The ``document_near_dups`` rows produced here
are pure metadata: they record which files *look* near-identical (a shared
``group_id`` when the cosine of their chunk-embedding centroids is at least
``settings.near_dup_threshold``) so surfaces can surface/annotate the relation.
Nothing in ingestion or retrieval ever acts on the grouping destructively.

Design:

- A file's centroid is the L2-normalized mean of its current-generation chunk
  embeddings (computed where those embeddings are already in memory — no
  re-embedding), stored as a float32 BLOB in ``document_near_dups`` (one row
  per file; ``file_id`` is UNIQUE so re-ingest replaces the row idempotently).
- Comparison is bounded: at most the ``MAX_COMPARE`` most-recent OTHER
  centroids in the same vault are scanned. Cosine of two L2-normalized
  vectors is their dot product (computed via the general cosine form anyway
  so abnormally-scaled blobs cannot skew results).
- Text-space fallback (issue #513 C25): files that pre-date centroid
  recording (migrated or directly seeded rows) have no centroid to compare
  against. When the ingested file's full text is available, each such file's
  advisory similarity is estimated from a DETERMINISTIC offline fingerprint
  of its ``parsed_text`` (L2-normalized token-count feature hashing — no
  model, no network), and above-threshold matches gain a centroid row in the
  same shared group. Purely additive and advisory.
- On a match at/above the threshold, the new row reuses the matched row's
  ``group_id`` when it has one (backfilling it if empty); otherwise a fresh
  ``uuid4().hex`` group is created. The best (highest-cosine) match wins.
- Every entry point is best-effort: ``record_file_centroid`` logs a warning
  and returns on ANY failure — callers run inside ingestion finalization and
  must never fail an otherwise-successful ingest over advisory metadata.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections.abc import Sequence
from uuid import uuid4

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# Bounded scan: at most this many most-recent other centroids are compared per
# ingest, keeping the advisory cost O(1) relative to vault size (plan W26).
MAX_COMPARE = 500

# Width of the deterministic offline text fingerprint (feature hashing). Wide
# enough that unrelated documents' token bags collide negligibly; fixed so
# fingerprints stay comparable across processes and runs.
FINGERPRINT_DIM = 256

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _text_fingerprint(text: str) -> np.ndarray | None:
    """Deterministic offline text fingerprint (L2-normalized token counts).

    Advisory fallback ONLY: estimates document similarity for files whose
    chunk embeddings are not in memory (pre-existing rows without a centroid).
    Feature-hashes lowercase word tokens into ``FINGERPRINT_DIM`` buckets — no
    model, no network, identical output for identical text in every process.
    Returns None for text with no usable tokens.
    """
    vec = np.zeros(FINGERPRINT_DIM, dtype=np.float32)
    for token in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        vec[int.from_bytes(digest, "big") % FINGERPRINT_DIM] += 1.0
    norm = float(np.linalg.norm(vec))
    if not np.isfinite(norm) or norm <= 0.0:
        return None
    return (vec / norm).astype(np.float32)


def _centroid_from_embeddings(embeddings: Sequence[Sequence[float]]) -> np.ndarray | None:
    """Mean-pool chunk embeddings into an L2-normalized float32 centroid.

    Returns None when the input carries no usable signal (empty, non-finite,
    or zero-norm), in which case the caller skips centroid recording entirely.
    """
    try:
        mat = np.asarray(embeddings, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if mat.ndim == 1:
        # A single embedding (not a list of them) — still a valid centroid.
        mat = mat.reshape(1, -1)
    if mat.ndim != 2 or mat.shape[0] == 0 or mat.shape[1] == 0:
        return None
    centroid = mat.mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    if not np.isfinite(norm) or norm <= 0.0:
        return None
    return (centroid / norm).astype(np.float32)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity with zero-norm guards (both inputs are 1-D)."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


def record_file_centroid(
    conn: sqlite3.Connection,
    vault_id: int,
    file_id: int,
    embeddings: Sequence[Sequence[float]],
    *,
    threshold: float | None = None,
    document_text: str | None = None,
) -> None:
    """Record (or replace) a file's centroid and assign an advisory group_id.

    Compares the file's pooled centroid against up to ``MAX_COMPARE`` the most
    recent other centroids in the same vault; on cosine >= threshold the file
    joins the matched row's group (reusing its group_id, backfilling it if
    empty, else minting a new ``uuid4().hex`` group) and stores the similarity
    against the best match. ADVISORY ONLY: never raises — any failure is
    logged as a warning and the ingest continues unaffected.

    Args:
        conn: Application sqlite connection (the caller's transaction/commit
            conventions apply; the advisory write is committed here so other
            connections can observe it).
        vault_id: Vault scope for the comparison.
        file_id: File whose centroid is being recorded.
        embeddings: The file's current-generation chunk embeddings.
        threshold: Cosine threshold; defaults to ``settings.near_dup_threshold``.
        document_text: The file's full parsed text. When provided, vault files
            that pre-date centroid recording (no ``document_near_dups`` row)
            are additionally compared via deterministic offline text
            fingerprints of their ``parsed_text``; above-threshold matches
            gain a centroid row in this file's group (issue #513 C25 —
            seeded/migrated documents must still join advisory groups).
            Omitted (None) by legacy callers: behavior identical to before.
    """
    try:
        centroid = _centroid_from_embeddings(embeddings)
        if centroid is None:
            return
        dim = int(centroid.shape[0])
        effective_threshold = (
            float(settings.near_dup_threshold) if threshold is None else float(threshold)
        )

        rows = conn.execute(
            "SELECT file_id, centroid, group_id FROM document_near_dups "
            "WHERE vault_id = ? AND file_id != ? "
            "ORDER BY computed_at DESC, id DESC LIMIT ?",
            (vault_id, file_id, MAX_COMPARE),
        ).fetchall()

        matched_file_id: int | None = None
        matched_group_id: str | None = None
        best_similarity = -1.0
        for other_file_id, blob, other_group_id in rows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            other = np.frombuffer(blob, dtype=np.float32)
            if other.shape[0] != dim:
                continue
            similarity = _cosine(centroid, other)
            if similarity >= effective_threshold and similarity > best_similarity:
                best_similarity = similarity
                matched_file_id = int(other_file_id)
                matched_group_id = other_group_id

        # Text-space fallback (issue #513 C25): estimate similarity for vault
        # files that pre-date centroid recording — they have no row to compare
        # embeddings against, so compare deterministic offline fingerprints of
        # their parsed_text against this file's. Bounded and advisory: each
        # above-threshold match gains one centroid row in the shared group.
        text_backfill: list[tuple[int, np.ndarray, float]] = []
        if document_text:
            fingerprint = _text_fingerprint(document_text)
            if fingerprint is not None:
                try:
                    candidates = conn.execute(
                        "SELECT id, parsed_text FROM files "
                        "WHERE vault_id = ? AND parsed_text IS NOT NULL "
                        "AND parsed_text != '' AND id != ? "
                        "AND id NOT IN (SELECT file_id FROM document_near_dups) "
                        "ORDER BY id DESC LIMIT ?",
                        (vault_id, file_id, MAX_COMPARE),
                    ).fetchall()
                except sqlite3.Error:
                    # Pre-parsed_text schema shape: the fallback is advisory,
                    # skip it without disturbing the embedding-space path.
                    candidates = []
                for other_id, parsed_text in candidates:
                    other_fingerprint = _text_fingerprint(str(parsed_text))
                    if other_fingerprint is None:
                        continue
                    similarity = _cosine(fingerprint, other_fingerprint)
                    if similarity >= effective_threshold:
                        text_backfill.append(
                            (int(other_id), other_fingerprint, float(similarity))
                        )

        if matched_file_id is not None:
            group_id = matched_group_id or uuid4().hex
            if not matched_group_id:
                # The matched row pre-dates group assignment — backfill so
                # both members of the pair carry the shared group.
                conn.execute(
                    "UPDATE document_near_dups SET group_id = ? WHERE file_id = ?",
                    (group_id, matched_file_id),
                )
            similarity_to_store: float | None = best_similarity
        elif text_backfill:
            # No live-centroid match, but similar pre-existing documents were
            # found by text: mint the shared group and give each of them a
            # centroid row so the advisory link is queryable both ways.
            group_id = uuid4().hex
            similarity_to_store = max(sim for _oid, _fp, sim in text_backfill)
        else:
            group_id = uuid4().hex
            similarity_to_store = None

        for other_id, other_fingerprint, similarity in text_backfill:
            conn.execute(
                "INSERT OR REPLACE INTO document_near_dups "
                "(vault_id, file_id, centroid, dim, group_id, similarity, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    vault_id,
                    other_id,
                    other_fingerprint.tobytes(),
                    int(other_fingerprint.shape[0]),
                    group_id,
                    similarity,
                ),
            )

        conn.execute(
            "INSERT OR REPLACE INTO document_near_dups "
            "(vault_id, file_id, centroid, dim, group_id, similarity, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (vault_id, file_id, centroid.tobytes(), dim, group_id, similarity_to_store),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "near-duplicate centroid recording failed for file %s "
            "(advisory only; ingest unaffected)",
            file_id,
            exc_info=True,
        )


def get_near_duplicate_group(conn: sqlite3.Connection, file_id: int) -> str | None:
    """Return the file's advisory near-duplicate group_id, or None."""
    row = conn.execute(
        "SELECT group_id FROM document_near_dups WHERE file_id = ?", (file_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def clear_file_centroid(conn: sqlite3.Connection, file_id: int) -> None:
    """Delete the file's advisory centroid row (document delete/purge flows)."""
    conn.execute("DELETE FROM document_near_dups WHERE file_id = ?", (file_id,))
