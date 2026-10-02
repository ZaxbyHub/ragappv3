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
- Centroids carry the ``embedding_model`` they were computed under (issue
  #697): only rows recorded under the same model at the same dimension are
  compared in embedding space. A same-dimension model switch therefore
  invalidates old rows instead of silently comparing across spaces.
- Text-space fallback (issue #513 C25, routing fixed by #697): vault files
  that are not embedding-comparable for a record — no row (pre-dating
  centroid recording), or a row under a different (model, dim) — are compared
  via a DETERMINISTIC offline fingerprint of their ``parsed_text``
  (L2-normalized token-count feature hashing — no model, no network).
  Stored fingerprints (dim=256, model NULL rows) are reused without
  re-tokenization, and every scanned candidate's row is written in
  fingerprint form, marking it scanned so no later ingest repeats the work;
  above-threshold matches gain a fingerprint row in the shared group.
  Purely additive and advisory.
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
    recent other centroids **recorded under the same embedding model at the
    same dimension** (issue #697: model identity is persisted per row, so a
    same-dimension model switch or a stale legacy row is never silently
    compared across incomparable spaces). On cosine >= threshold the file joins
    the matched row's group (reusing its group_id, backfilling it if empty,
    else minting a new ``uuid4().hex`` group) and stores the similarity against
    the best match. ADVISORY ONLY: never raises — any failure is logged as a
    warning and the ingest continues unaffected.

    Args:
        conn: Application sqlite connection (the caller's transaction/commit
            conventions apply; the advisory write is committed here so other
            connections can observe it).
        vault_id: Vault scope for the comparison.
        file_id: File whose centroid is being recorded.
        embeddings: The file's current-generation chunk embeddings.
        threshold: Cosine threshold; defaults to ``settings.near_dup_threshold``.
        document_text: The file's full parsed text. When provided, vault files
            that are NOT embedding-comparable for this record — no
            ``document_near_dups`` row at all, or a row recorded under a
            different (model, dim) — are compared via deterministic offline
            text fingerprints of their ``parsed_text`` (issue #513 C25; issue
            #697 turns the old dimension-skip into this explicit
            re-fingerprint route so no file is unreachable by both scan
            paths). Stored fingerprints (dim=256, model NULL rows) are reused
            without re-tokenization; every other scanned candidate is
            fingerprinted at most ONCE and its row is written/rewritten in
            fingerprint form (``embedding_model`` NULL, dim 256), which marks
            it scanned so no later ingest re-fingerprints it. Above-threshold
            matches join the shared group (a matched candidate's existing
            group_id is reused when non-NULL, else a fresh group is minted and
            backfilled onto both rows). Omitted (None) by legacy callers:
            behavior identical to before, plus the model-identity filter.
    """
    try:
        centroid = _centroid_from_embeddings(embeddings)
        if centroid is None:
            return
        dim = int(centroid.shape[0])
        model = str(settings.embedding_model)
        effective_threshold = (
            float(settings.near_dup_threshold) if threshold is None else float(threshold)
        )

        rows = conn.execute(
            "SELECT file_id, centroid, group_id FROM document_near_dups "
            "WHERE vault_id = ? AND file_id != ? "
            "AND embedding_model = ? AND dim = ? "
            "ORDER BY computed_at DESC, id DESC LIMIT ?",
            (vault_id, file_id, model, dim, MAX_COMPARE),
        ).fetchall()

        matched_file_id: int | None = None
        matched_group_id: str | None = None
        best_similarity = -1.0
        for other_file_id, blob, other_group_id in rows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            other = np.frombuffer(blob, dtype=np.float32)
            if other.shape[0] != dim:
                # Defensive only: the SQL filter already pins model+dim, so a
                # mismatch here means a corrupted blob, not a legacy row.
                continue
            similarity = _cosine(centroid, other)
            if similarity >= effective_threshold and similarity > best_similarity:
                best_similarity = similarity
                matched_file_id = int(other_file_id)
                matched_group_id = other_group_id

        # Text-space fallback (issue #513 C25, routing fixed by #697): compare
        # deterministic offline fingerprints for vault files that are NOT
        # embedding-comparable for this record — no row, or a row recorded
        # under a different (model, dim). Stored fingerprints (dim=256, model
        # NULL) are reused as-is; anything else is fingerprinted once and its
        # row written in fingerprint form, which marks the file scanned so no
        # later ingest repeats the tokenization.
        text_backfill: list[tuple[int, np.ndarray, float, str | None]] = []
        scan_marks: list[tuple[int, np.ndarray, str | None]] = []
        if document_text:
            fingerprint = _text_fingerprint(document_text)
            if fingerprint is not None:
                try:
                    # Scan-marked files (stored fingerprints) must not pay the
                    # full-text transfer again on every later ingest (issue
                    # #697): both the select list and the != '' predicate
                    # route stored-fingerprint rows through a constant so
                    # SQLite never materializes their parsed_text; first-time
                    # compute candidates still get their full text.
                    candidates = conn.execute(
                        "SELECT files.id, "
                        "CASE WHEN nd.dim = ? AND nd.embedding_model IS NULL "
                        "THEN NULL ELSE files.parsed_text END, "
                        "nd.centroid, nd.group_id, nd.embedding_model, nd.dim "
                        "FROM files LEFT JOIN document_near_dups nd "
                        "ON nd.file_id = files.id "
                        "WHERE files.vault_id = ? "
                        "AND (CASE WHEN nd.dim = ? AND nd.embedding_model IS NULL "
                        "THEN 'marked' ELSE files.parsed_text END) != '' "
                        "AND files.id != ? "
                        "AND NOT EXISTS (SELECT 1 FROM document_near_dups c "
                        "WHERE c.file_id = files.id AND c.embedding_model = ? "
                        "AND c.dim = ?) "
                        "ORDER BY files.id DESC LIMIT ?",
                        (
                            FINGERPRINT_DIM,
                            vault_id,
                            FINGERPRINT_DIM,
                            file_id,
                            model,
                            dim,
                            MAX_COMPARE,
                        ),
                    ).fetchall()
                except sqlite3.Error:
                    # Pre-parsed_text schema shape: the fallback is advisory,
                    # skip it without disturbing the embedding-space path.
                    candidates = []
                for other_id, parsed_text, blob, other_group, other_model, other_dim in candidates:
                    other_fingerprint: np.ndarray | None = None
                    if (
                        blob is not None
                        and other_model is None
                        and other_dim == FINGERPRINT_DIM
                        and isinstance(blob, (bytes, bytearray))
                    ):
                        stored = np.frombuffer(blob, dtype=np.float32)
                        if stored.shape[0] == FINGERPRINT_DIM:
                            other_fingerprint = stored
                    needs_mark = other_fingerprint is None
                    if other_fingerprint is None:
                        if parsed_text is None:
                            # A stored-fingerprint row whose blob failed the
                            # shape check: no text was fetched for it, and a
                            # corrupt fingerprint is not worth re-deriving —
                            # leave it for a future re-scan wave.
                            continue
                        other_fingerprint = _text_fingerprint(str(parsed_text))
                    if other_fingerprint is None:
                        continue
                    similarity = _cosine(fingerprint, other_fingerprint)
                    if similarity >= effective_threshold:
                        text_backfill.append(
                            (int(other_id), other_fingerprint, float(similarity), other_group)
                        )
                    elif needs_mark:
                        # Scanned once with no match: mark the file so no
                        # later ingest re-tokenizes it (rewrite preserves any
                        # existing advisory group membership).
                        scan_marks.append((int(other_id), other_fingerprint, other_group))

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
            # No live-centroid match, but similar documents were found by
            # text: reuse the best match's existing group when it has one
            # (highest cosine first, so the pair's group follows its closest
            # member), else mint the shared group and give each of them a
            # fingerprint row so the advisory link is queryable both ways.
            group_id = next(
                (
                    g
                    for _oid, _fp, _sim, g in sorted(
                        text_backfill, key=lambda entry: entry[2], reverse=True
                    )
                    if g is not None
                ),
                uuid4().hex,
            )
            similarity_to_store = max(sim for _oid, _fp, sim, _g in text_backfill)
        else:
            group_id = uuid4().hex
            similarity_to_store = None

        for other_id, other_fingerprint, _similarity, _existing_group in text_backfill:
            conn.execute(
                "INSERT OR REPLACE INTO document_near_dups "
                "(vault_id, file_id, centroid, dim, embedding_model, group_id, "
                "similarity, computed_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, CURRENT_TIMESTAMP)",
                (
                    vault_id,
                    other_id,
                    other_fingerprint.tobytes(),
                    int(other_fingerprint.shape[0]),
                    group_id,
                    _similarity,
                ),
            )

        for other_id, other_fingerprint, existing_group in scan_marks:
            conn.execute(
                "INSERT OR REPLACE INTO document_near_dups "
                "(vault_id, file_id, centroid, dim, embedding_model, group_id, "
                "similarity, computed_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, NULL, CURRENT_TIMESTAMP)",
                (
                    vault_id,
                    other_id,
                    other_fingerprint.tobytes(),
                    int(other_fingerprint.shape[0]),
                    existing_group,
                ),
            )

        conn.execute(
            "INSERT OR REPLACE INTO document_near_dups "
            "(vault_id, file_id, centroid, dim, embedding_model, group_id, "
            "similarity, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (vault_id, file_id, centroid.tobytes(), dim, model, group_id, similarity_to_store),
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
