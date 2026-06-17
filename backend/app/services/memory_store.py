"""Memory storage service.

Storage layers:
  * SQLite + FTS5 — durable lexical index (always-on).
  * Optional dense embeddings stored as JSON in ``memories.embedding``.

Retrieval flow:
  1. FTS5 lexical search returns the top-K lexical matches (always works).
  2. If an embedding service is wired in and the memory rows have stored
     embeddings, dense search returns the top-K cosine-similar memories.
  3. Both lists are fused via Reciprocal Rank Fusion. Result records
     carry both ``score`` and ``score_type`` ("rrf" when fused, "fts" when
     fallback only).

Embedding generation is opportunistic: if the embedding service is
unavailable when a memory is added or updated, the row is still
persisted and indexed lexically. A later background pass (or a fresh
``add_memory`` call) can populate the embedding without reindexing.
"""

import asyncio
import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, List, Optional

from app.config import settings
from app.models.database import SQLiteConnectionPool
from app.utils.fusion import rrf_fuse
from app.utils.retry import with_retry

logger = logging.getLogger(__name__)


# Stop words stripped before building the FTS5 AND query. Natural-language
# questions ("who is the afomis chief?") contain these tokens, which are not
# in memory content and cause implicit-AND matches to fail entirely.
_FTS_STOP_WORDS = frozenset(
    "who what when where why how is are was were the a an of for to in on about".split()
)


class MemoryStoreError(Exception):
    """General memory store error."""


class MemoryDetectionError(MemoryStoreError):
    """Raised when a memory pattern cannot be parsed."""


@dataclass
class MemoryRecord:
    id: int
    content: str
    category: Optional[str]
    tags: Optional[str]
    source: Optional[str]
    vault_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    score: Optional[float] = None
    # "fts" — pure lexical match
    # "dense" — pure embedding similarity
    # "rrf" — Reciprocal Rank Fusion of the two
    score_type: Optional[str] = None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length float vectors.

    Returns 0.0 for length mismatch or zero vectors so callers don't have
    to special-case those edge conditions.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class MemoryStore:
    """Provides memory storage and retrieval backed by SQLite + FTS5."""

    # Each entry's regex captures the memory body in the named group
    # ``memory``. Patterns terminate at one of:
    #   * ``.``, ``!``, ``?`` (sentence-final punctuation)
    #   * end of string
    #
    # The list is anchored with ``^`` and a soft start-of-line lookbehind
    # (using a leading word-boundary clause) so we only match phrases the
    # user issued *as the imperative* — not embedded incidentally inside
    # quoted text such as "the document says, 'note that ...'".
    MEMORY_PATTERNS = [
        re.compile(
            r"(?:^|\s)(?:please\s+)?remember\s+(?:that|to)\s+(?P<memory>.+?)(?:[.!?](?:\s|$)|$)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"(?:^|\s)don'?t\s+forget\s+(?:that\s+)?(?P<memory>.+?)(?:[.!?](?:\s|$)|$)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"(?:^|\s)keep\s+in\s+mind\s+(?:that\s+)?(?P<memory>.+?)(?:[.!?](?:\s|$)|$)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"(?:^|\s)(?:please\s+)?note\s+that\s+(?P<memory>.+?)(?:[.!?](?:\s|$)|$)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"(?:^|\s)save\s+(?:this\s+)?(?:as\s+(?:a\s+)?memory)\s*[:\-]?\s*(?P<memory>.+?)(?:[.!?](?:\s|$)|$)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"(?:^|\s)my\s+preference\s+is\s+(?:that\s+)?(?P<memory>.+?)(?:[.!?](?:\s|$)|$)",
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    # Phrases that strongly suggest the text is *quoting* or *describing*
    # someone else's note rather than issuing a memory directive.
    _QUOTE_GUARD_RE = re.compile(
        r"(the\s+(document|article|paper|source|author|report)|they\s+(say|noted|wrote)|according\s+to|"
        r"the\s+text\s+(says|reads|notes))",
        re.IGNORECASE,
    )

    def __init__(
        self,
        pool: Optional[SQLiteConnectionPool] = None,
        embedding_service: Optional[Any] = None,
    ) -> None:
        if pool is None:
            pool = SQLiteConnectionPool(str(settings.sqlite_path), max_size=settings.memory_store_pool_size)
        self.pool = pool
        # Optional. When None or when its calls fail, we silently fall back
        # to FTS-only retrieval so memory features still work in
        # environments without a live embedding server.
        self.embedding_service = embedding_service

    def _has_embedding_columns(self, conn: sqlite3.Connection) -> bool:
        """Detect whether the optional ``embedding`` column is present.

        Cheap and idempotent; SQLite caches table_info results internally.
        """
        cursor = conn.execute("PRAGMA table_info(memories)")
        return any(row[1] == "embedding" for row in cursor.fetchall())

    async def _embed_text(self, text: str) -> Optional[List[float]]:
        """Best-effort embed; never raises. Returns None on failure or when
        no embedding service is wired in.
        """
        if not self.embedding_service or not text:
            return None
        try:
            return await self.embedding_service.embed_passage(text)
        except Exception as exc:  # noqa: BLE001 — defensive, optional path
            logger.debug("Memory embedding failed (continuing FTS-only): %s", exc)
            return None

    def _store_embedding(
        self, memory_id: int, embedding: Optional[List[float]]
    ) -> None:
        """Persist or clear the embedding JSON for a single memory row."""
        if embedding is None:
            return
        try:
            payload = json.dumps(embedding)
            model = getattr(settings, "embedding_model", None) or ""
            conn = self.pool.get_connection()
            try:
                if not self._has_embedding_columns(conn):
                    return
                conn.execute(
                    "UPDATE memories SET embedding = ?, embedding_model = ? WHERE id = ?",
                    (payload, model, memory_id),
                )
                conn.commit()
            finally:
                self.pool.release_connection(conn)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to store memory embedding (id=%s): %s", memory_id, exc)

    def close_all(self) -> None:
        """Close all connections in this MemoryStore's dedicated pool."""
        self.pool.close_all()

    async def embed_and_store(self, memory_id: int, content: str) -> None:
        """Public helper: compute and persist the embedding for an existing memory.

        Useful for backfilling memories that pre-date the embedding column,
        or for re-running after the embedding model changes. No-op if no
        embedding service is configured.
        """
        embedding = await self._embed_text(content)
        if embedding is not None:
            await asyncio.to_thread(self._store_embedding, memory_id, embedding)

    async def backfill_missing_embeddings(self, batch_size: int = 50) -> dict:
        """Idempotent backfill: embed memories that have no embedding or whose
        embedding was generated by a different model than the current one.

        Runs in the background; does not block startup. If the embedding service
        is unavailable the run is logged as skipped and FTS fallback remains intact.

        Returns a summary dict with counts of processed/skipped/failed rows.
        """
        from app.config import settings as _settings

        current_model = _settings.embedding_model
        summary = {"processed": 0, "skipped": 0, "failed": 0, "total": 0}

        if self.embedding_service is None:
            logger.info("Memory embedding backfill skipped: no embedding service configured")
            return summary

        conn = self.pool.get_connection()
        try:
            if not self._has_embedding_columns(conn):
                logger.info("Memory embedding backfill skipped: embedding columns not present")
                return summary

            cursor = conn.execute(
                "SELECT id, content FROM memories WHERE embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?",
                (current_model,),
            )
            rows = cursor.fetchall()
        finally:
            self.pool.release_connection(conn)

        summary["total"] = len(rows)
        if not rows:
            logger.info("Memory embedding backfill: nothing to do (all memories up to date)")
            return summary

        logger.info(
            "Memory embedding backfill starting: %d memories need embedding (model=%s)",
            len(rows),
            current_model,
        )

        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            for memory_id, content in batch:
                try:
                    await self.embed_and_store(memory_id, content)
                    summary["processed"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Backfill failed for memory %d: %s", memory_id, exc)
                    summary["failed"] += 1

            logger.info(
                "Memory embedding backfill progress: %d/%d done",
                min(i + batch_size, len(rows)),
                len(rows),
            )
            # Yield control between batches so we don't starve other coroutines
            await asyncio.sleep(0)

        logger.info(
            "Memory embedding backfill complete: processed=%d failed=%d",
            summary["processed"],
            summary["failed"],
        )
        return summary

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def add_memory(
        self,
        content: str,
        category: Optional[str] = None,
        tags: Optional[str] = None,
        source: Optional[str] = None,
        vault_id: Optional[int] = None,
    ) -> MemoryRecord:
        if not content or not content.strip():
            raise MemoryStoreError("Memory content cannot be empty")

        sql = """
        INSERT INTO memories (content, category, tags, source, vault_id)
        VALUES (?, ?, ?, ?, ?)
        """
        conn = self.pool.get_connection()
        try:
            cursor = conn.execute(sql, (content, category, tags, source, vault_id))
            conn.commit()
            memory_id = cursor.lastrowid
            if memory_id is None:
                raise MemoryStoreError("Failed to insert memory")
            # Fetch created_at, updated_at, and vault_id for the inserted row
            cursor = conn.execute(
                "SELECT created_at, updated_at, vault_id FROM memories WHERE id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            created_at = row[0] if row else None
            updated_at = row[1] if row else None
            retrieved_vault_id = row[2] if row else None
        finally:
            self.pool.release_connection(conn)

        # Best-effort embedding generation. We compute the embedding via a
        # synchronous bridge — callers that already provide an event loop
        # should use ``embed_and_store`` directly. Failures are swallowed
        # because lexical search continues to work without the embedding.
        if self.embedding_service is not None:
            try:
                embedding = asyncio.run(self._embed_text(content))
                if embedding is not None:
                    self._store_embedding(memory_id, embedding)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Memory embedding skipped on add (id=%s): %s", memory_id, exc
                )

        return MemoryRecord(
            id=memory_id,
            content=content,
            category=category,
            tags=tags,
            source=source,
            vault_id=retrieved_vault_id,
            created_at=created_at,
            updated_at=updated_at,
        )

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def update_memory_content(self, memory_id: int, new_content: str) -> None:
        """Update a memory's content + reset its embedding so the next
        retrieval pass either uses the freshly recomputed embedding
        (best-effort here) or falls back to FTS for the row.
        """
        if not new_content or not new_content.strip():
            raise MemoryStoreError("Memory content cannot be empty")
        conn = self.pool.get_connection()
        try:
            if self._has_embedding_columns(conn):
                conn.execute(
                    "UPDATE memories SET content = ?, embedding = NULL, embedding_model = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_content, memory_id),
                )
            else:
                conn.execute(
                    "UPDATE memories SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_content, memory_id),
                )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        if self.embedding_service is not None:
            try:
                embedding = asyncio.run(self._embed_text(new_content))
                if embedding is not None:
                    self._store_embedding(memory_id, embedding)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Memory embedding refresh skipped (id=%s): %s", memory_id, exc
                )

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def delete_memory(self, memory_id: int) -> None:
        """Delete a memory row. The embedding is implicitly removed via the
        same row delete; FTS5 cleanup is handled by the existing trigger.
        """
        conn = self.pool.get_connection()
        try:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    @with_retry(max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True)
    def _fts_search(
        self, query: str, limit: int, vault_id: Optional[int]
    ) -> List[MemoryRecord]:
        """FTS5 lexical search. Returns up to ``limit`` rows ordered by rank.

        Always works (no embedding service required). Used both as the
        primary path when no embedding service is configured and as one
        side of the hybrid fusion when one is.

        Vault scoping:
            ``vault_id=None`` — scan ALL vaults (admin/global mode, no filter).
            ``vault_id=<int>`` — filter to that vault OR ``vault_id IS NULL``
            (includes vault-less memories shared across all vaults).
        """
        if not query or not query.strip():
            return []

        # Tokenize and remove stop words so natural-language questions like
        # "who is the afomis chief?" produce a valid FTS5 AND query ("afomis
        # chief") rather than failing on punctuation or stop-word tokens that
        # are absent from memory content.
        tokens = [
            t for t in re.findall(r'[a-zA-Z0-9]+', query)
            if t.lower() not in _FTS_STOP_WORDS
        ]
        if not tokens:
            return []
        sanitized_query = ' '.join(tokens)

        conn = self.pool.get_connection()
        try:
            try:
                if vault_id is None:
                    sql = """
                    SELECT m.id, m.content, m.category, m.tags, m.source, m.vault_id, m.created_at, m.updated_at, f.rank
                    FROM memories_fts f
                    JOIN memories m ON f.rowid = m.id
                    WHERE memories_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """
                    params: tuple = (sanitized_query, limit)
                else:
                    sql = """
                    SELECT m.id, m.content, m.category, m.tags, m.source, m.vault_id, m.created_at, m.updated_at, f.rank
                    FROM memories_fts f
                    JOIN memories m ON f.rowid = m.id
                    WHERE memories_fts MATCH ?
                    AND (m.vault_id = ? OR m.vault_id IS NULL)
                    ORDER BY rank
                    LIMIT ?
                    """
                    params = (sanitized_query, vault_id, limit)

                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()
            except sqlite3.Error as e:
                raise MemoryStoreError(f"FTS query failed: {e}")
        finally:
            self.pool.release_connection(conn)

        records: List[MemoryRecord] = []
        for row in rows:
            record = MemoryRecord(
                id=row[0],
                content=row[1],
                category=row[2],
                tags=row[3],
                source=row[4],
                vault_id=row[5],
                created_at=row[6],
                updated_at=row[7],
                score=row[8],
                score_type="fts",
            )
            records.append(record)
        return records

    def _dense_search(
        self,
        query_embedding: List[float],
        limit: int,
        vault_id: Optional[int],
        candidate_ids: Optional[List[int]] = None,
    ) -> List[MemoryRecord]:
        """Cosine-similarity dense search across vault-scoped memories.

        Returns up to ``limit`` rows ordered by similarity descending.
        Memories without a stored embedding are skipped. Performs the
        comparison in Python to avoid a vector extension dependency —
        memory volume per vault is typically small (< 10k rows).

        When ``candidate_ids`` is provided, only memories with those IDs
        are considered (pre-filtered by FTS search). Otherwise all memories
        with embeddings are considered.

        Vault scoping:
            ``vault_id=None`` — scan ALL vaults (admin/global mode, no filter).
            ``vault_id=<int>`` — filter to that vault OR ``vault_id IS NULL``
            (includes vault-less memories shared across all vaults).
        """
        if not query_embedding:
            return []

        conn = self.pool.get_connection()
        try:
            if not self._has_embedding_columns(conn):
                return []
            if vault_id is None:
                sql = """
                SELECT id, content, category, tags, source, vault_id, created_at, updated_at, embedding
                FROM memories WHERE embedding IS NOT NULL
                """
                params: tuple = ()
            else:
                sql = """
                SELECT id, content, category, tags, source, vault_id, created_at, updated_at, embedding
                FROM memories
                WHERE embedding IS NOT NULL
                  AND (vault_id = ? OR vault_id IS NULL)
                """
                params = (vault_id,)
            if candidate_ids:
                placeholders = ','.join('?' * len(candidate_ids))
                sql += f" AND id IN ({placeholders})"
                params += tuple(candidate_ids)
                sql += " ORDER BY id DESC LIMIT ?"
                params += (limit * 3,)
            else:
                # Full scan: bounded by ``memory_dense_max_candidates`` (ordered by
                # recency before similarity ranking) to prevent unbounded Python-side
                # cosine work while giving semantic recall a wide pool. Dense is now
                # the sole semantic-recall path (no longer pre-filtered to FTS hits),
                # so this bound is the recall ceiling for very large vaults.
                sql += " ORDER BY id DESC LIMIT ?"
                params += (max(limit * 3, settings.memory_dense_max_candidates),)
            rows = conn.execute(sql, params).fetchall()
        finally:
            self.pool.release_connection(conn)

        scored: List[MemoryRecord] = []
        for row in rows:
            try:
                vec = json.loads(row[8]) if row[8] else None
            except (TypeError, json.JSONDecodeError):
                vec = None
            if not isinstance(vec, list):
                continue
            sim = _cosine_similarity(query_embedding, vec)
            min_sim = settings.memory_dense_min_similarity if settings.memory_relevance_filter_enabled else 0.0
            if sim <= min_sim:
                continue
            scored.append(
                MemoryRecord(
                    id=row[0],
                    content=row[1],
                    category=row[2],
                    tags=row[3],
                    source=row[4],
                    vault_id=row[5],
                    created_at=row[6],
                    updated_at=row[7],
                    score=sim,
                    score_type="dense",
                )
            )
        scored.sort(key=lambda r: (r.score or 0.0), reverse=True)
        return scored[:limit]

    def search_memories(
        self, query: str, limit: int = 5, vault_id: Optional[int] = None
    ) -> List[MemoryRecord]:
        """Hybrid memory retrieval: FTS5 lexical + dense semantic + RRF fusion.

        Falls back to FTS-only when:
          * no embedding service is configured;
          * the embedding service raises;
          * no memory rows in the vault have stored embeddings.

        ``score_type`` on returned records reflects which path produced
        them: ``"rrf"`` when fusion happened, ``"fts"`` or ``"dense"``
        otherwise.
        """
        # FTS results — always run.
        fts_records = self._fts_search(query, limit, vault_id)

        # Dense results — best-effort, run UNFILTERED across the vault.
        # Synchronously embed when we already have an event loop (this method
        # is invoked via to_thread from async code). Never let dense errors
        # break the call.
        dense_records: List[MemoryRecord] = []
        if self.embedding_service is not None and query and query.strip():
            try:
                # The embedding service exposes async methods; bridge them
                # to this sync entry point using a fresh event loop only
                # when we're not already inside one. The RAG engine calls
                # ``search_memories`` via ``asyncio.to_thread``, so we are
                # always on a worker thread without a current loop here.
                query_emb = asyncio.run(self.embedding_service.embed_single(query))
            except Exception as exc:  # noqa: BLE001 — best effort
                logger.debug(
                    "Memory dense embedding failed; falling back to FTS-only: %s",
                    exc,
                )
                query_emb = None
            if query_emb:
                # Run dense UNFILTERED (no candidate_ids restriction) so a
                # semantically-relevant memory sharing no lexical tokens with the
                # query can still surface. Previously dense was pre-filtered to the
                # FTS hit set, which made semantic-only recall impossible whenever
                # FTS returned any candidate. RRF fusion below still rewards
                # memories found by both arms.
                dense_records = self._dense_search(query_emb, limit, vault_id)

        # No dense path → return FTS as-is.
        if not dense_records:
            return fts_records[:limit]
        # No FTS path → return dense as-is.
        if not fts_records:
            return dense_records[:limit]

        # Both populated — fuse via RRF on memory id.
        fts_dicts = [{"id": str(r.id), "_rec": r} for r in fts_records]
        dense_dicts = [{"id": str(r.id), "_rec": r} for r in dense_records]
        fused = rrf_fuse(
            [fts_dicts, dense_dicts],
            k=settings.memory_rrf_k,
            limit=limit,
        )
        min_rrf = settings.memory_rrf_min_score if settings.memory_relevance_filter_enabled else 0.0
        out: List[MemoryRecord] = []
        for f in fused:
            rrf_score = float(f.get("_rrf_score", 0.0))
            if rrf_score <= min_rrf:
                continue
            rec: MemoryRecord = f["_rec"]
            # Replace the path-specific score with the fused RRF score so
            # callers see a single, ordering-meaningful value.
            rec.score = rrf_score
            rec.score_type = "rrf"
            out.append(rec)
        return out

    def detect_memory_intent(self, text: str) -> Optional[str]:
        """Detect a memory-store directive in user text.

        Recognises imperative phrasings ("remember that…", "don't forget
        to…", "keep in mind…", "note that…", "save as memory: …", "my
        preference is…"). Returns the captured body with trailing
        punctuation stripped, or ``None`` when no directive is present
        OR when the text is structured like a quotation/description of
        someone else's note (heuristic guard against false positives in
        document-content contexts).
        """
        if not text or not text.strip():
            return None

        # Heuristic: if the message is clearly *quoting* or *describing*
        # external content that contains a "note that" prefix, suppress
        # capture. We require both a quote-guard hit AND a colon/quote
        # nearby to keep the guard tight.
        guard_match = self._QUOTE_GUARD_RE.search(text)
        if guard_match:
            window = text[max(0, guard_match.start() - 8) : guard_match.end() + 60]
            if any(ch in window for ch in (':', '"', "'", "“", "”")):
                return None

        # Try each pattern in declaration order; return the first hit so
        # earlier (more imperative) phrasings win when multiple match.
        for pattern in self.MEMORY_PATTERNS:
            match = pattern.search(text)
            if match and match.groupdict().get("memory"):
                memory_content = match.group("memory").strip()
                # Strip wrapping quotes / trailing punctuation that the
                # capture preserved.
                memory_content = memory_content.strip("\"'“”‘’ \t\n")
                memory_content = memory_content.rstrip(".!?,;:")
                memory_content = memory_content.strip()
                if memory_content:
                    return memory_content
        return None



