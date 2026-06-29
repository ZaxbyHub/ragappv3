"""Feedback-driven reranking: boosts/demotes retrieved documents based on past user feedback."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# Default bonus per net-positive vote (applied as additive score adjustment).
_DEFAULT_BONUS_PER_VOTE = 0.05
# Hard cap on the feedback adjustment magnitude so it never overwhelms semantic scores.
_DEFAULT_MAX_BONUS = 0.10
# Cache time-to-live in seconds before a background refresh is triggered.
_CACHE_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class FeedbackScore:
    """Immutable feedback score for a single file_id."""

    file_id: str
    net_positive: int  # up_votes - down_votes
    up_votes: int
    down_votes: int
    computed_at: float  # unix timestamp


class FeedbackReranker:
    """Reranks retrieval results using historical user feedback on chat messages.

    The signal flows:
      chat_messages (feedback: up/down)
        → messages whose sources JSON cited a given file_id
        → net_positive_count per file_id
        → additive score bonus clamped to ±max_bonus.

    Caching: feedback scores are cached per vault_id with a TTL.  Threadsafety
    is guaranteed by a lock (one refresh at a time per vault).

    Parameters
    ----------
    db_path : str | None
        Path to the SQLite database.  Defaults to ``settings.sqlite_path``.
    bonus_per_vote : float
        Score adjustment added/subtracted per net-positive vote.  Bounded by
        ``max_bonus``.  Default: 0.05.
    max_bonus : float
        Absolute maximum magnitude of the feedback adjustment.  Default: 0.10.
    cache_ttl_seconds : int
        How long a cached score dict remains valid before requiring re-query.
        Default: 300 s.
    vault_scope : bool
        When True (the default), feedback is scoped to the query's vault so
        feedback from one vault cannot influence another vault's rankings.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        bonus_per_vote: float = _DEFAULT_BONUS_PER_VOTE,
        max_bonus: float = _DEFAULT_MAX_BONUS,
        cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
        vault_scope: bool = True,
        uri: bool = False,
    ) -> None:
        self._db_path = db_path or str(settings.sqlite_path)
        self._bonus_per_vote = bonus_per_vote
        self._max_bonus = max_bonus
        self._cache_ttl = cache_ttl_seconds
        self._vault_scope = vault_scope
        self._uri = uri

        # {vault_id: {file_id: FeedbackScore}}
        self._cache: Dict[Optional[int], Dict[str, FeedbackScore]] = {}
        # {vault_id: timestamp when cache was last populated}
        self._cache_timestamp: Dict[Optional[int], float] = {}
        # Tracks which vault_ids have been populated via get_feedback_score.
        # Used by refresh() to distinguish "never queried" from "queried but empty".
        self._seen_vault_ids: set = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------

    @staticmethod
    def _build_feedback_query(vault_scope: bool) -> Tuple[str, str]:
        """Return (sql_query, sql_params) that computes net-positive feedback per file_id.

        The query:
          1. Joins chat_messages → chat_sessions to obtain vault_id.
          2. Filters assistant messages with non-null, non-empty feedback.
          3. Parses the JSON sources array to extract individual file_id entries.
          4. Aggregates up/down votes per file_id per vault.

        Returns a row for every (vault_id, file_id) pair with:
          - up_votes   : COUNT of messages with feedback = 'up'
          - down_votes : COUNT of messages with feedback = 'down'
        """
        if vault_scope:
            select_clause = """
                SELECT
                    cs.vault_id,
                    json_extract(src.value, '$.file_id') AS file_id,
                    SUM(CASE WHEN cm.feedback = 'up'   THEN 1 ELSE 0 END) AS up_votes,
                    SUM(CASE WHEN cm.feedback = 'down' THEN 1 ELSE 0 END) AS down_votes
                FROM chat_messages cm
                JOIN chat_sessions cs ON cs.id = cm.session_id
                JOIN json_each(cm.sources) AS src
                WHERE cm.role = 'assistant'
                  AND cm.feedback IS NOT NULL
                  AND cm.feedback != ''
                  AND json_extract(src.value, '$.file_id') IS NOT NULL
                  AND json_extract(src.value, '$.file_id') != ''
                GROUP BY cs.vault_id, json_extract(src.value, '$.file_id')
            """
        else:
            select_clause = """
                SELECT
                    NULL AS vault_id,
                    json_extract(src.value, '$.file_id') AS file_id,
                    SUM(CASE WHEN cm.feedback = 'up'   THEN 1 ELSE 0 END) AS up_votes,
                    SUM(CASE WHEN cm.feedback = 'down' THEN 1 ELSE 0 END) AS down_votes
                FROM chat_messages cm
                JOIN json_each(cm.sources) AS src
                WHERE cm.role = 'assistant'
                  AND cm.feedback IS NOT NULL
                  AND cm.feedback != ''
                  AND json_extract(src.value, '$.file_id') IS NOT NULL
                  AND json_extract(src.value, '$.file_id') != ''
                GROUP BY json_extract(src.value, '$.file_id')
            """
        return select_clause, ()

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _is_cache_valid(self, vault_id: Optional[int]) -> bool:
        """Return True when the cache for vault_id is populated and not stale."""
        cache_key: Optional[int] = None if not self._vault_scope else vault_id
        ts = self._cache_timestamp.get(cache_key)
        if ts is None:
            return False
        return (time.monotonic() - ts) < self._cache_ttl

    def _ensure_cache_populated(self, vault_id: Optional[int]) -> None:
        """Ensure the per-vault cache is populated (thread-safe, idempotent)."""
        if self._is_cache_valid(vault_id):
            return
        with self._lock:
            # Double-check after acquiring the lock
            if self._is_cache_valid(vault_id):
                return
            self._refresh_cache_for_vault(vault_id, record_seen=True)

    def _refresh_cache_for_vault(self, vault_id: Optional[int], record_seen: bool = False) -> None:
        """Re-query the database and rebuild the cache for vault_id."""
        query_sql, query_params = self._build_feedback_query(self._vault_scope)

        try:
            conn = sqlite3.connect(self._db_path, uri=self._uri)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query_sql, query_params).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("FeedbackReranker: cache refresh failed for vault=%s: %s", vault_id, exc)
            return

        scores_map: Dict[str, FeedbackScore] = {}
        now = time.monotonic()
        for row in rows:
            row_vault_id: Optional[int] = row["vault_id"]
            # When vault_scope=True we get per-vault rows; when False we get NULL vault_id
            if self._vault_scope and row_vault_id != vault_id:
                continue
            file_id = row["file_id"]
            up_votes = row["up_votes"]
            down_votes = row["down_votes"]
            net = up_votes - down_votes
            scores_map[file_id] = FeedbackScore(
                file_id=file_id,
                net_positive=net,
                up_votes=up_votes,
                down_votes=down_votes,
                computed_at=now,
            )

        # When vault_scope=False the feedback is global (no per-vault breakdown),
        # so store it under the None key regardless of which vault_id was requested.
        cache_key: Optional[int] = None if not self._vault_scope else vault_id
        if record_seen:
            self._seen_vault_ids.add(cache_key)
        self._cache[cache_key] = scores_map
        self._cache_timestamp[cache_key] = now
        logger.debug(
            "FeedbackReranker: cached %d file_ids for vault=%s",
            len(scores_map),
            vault_id,
        )

    def refresh(self, vault_id: Optional[int]) -> None:
        """Force an immediate cache refresh for vault_id (bypasses TTL check).

        Refreshing a vault that has never been queried (never appeared via
        get_feedback_score / rerank) is a no-op to avoid creating empty stub
        entries for vaults that were never actually requested.
        """
        if self._vault_scope:
            if vault_id not in self._seen_vault_ids:
                return
        else:
            if None not in self._seen_vault_ids:
                return
        with self._lock:
            self._refresh_cache_for_vault(vault_id)

    # ------------------------------------------------------------------
    # Score computation
    # ------------------------------------------------------------------

    def _feedback_bonus(self, file_id: str, vault_id: Optional[int]) -> float:
        """Return the bounded additive bonus for file_id (from cache)."""
        self._ensure_cache_populated(vault_id)
        cache_key: Optional[int] = None if not self._vault_scope else vault_id
        scores = self._cache.get(cache_key, {})
        fb = scores.get(file_id)
        if fb is None:
            return 0.0
        raw = fb.net_positive * self._bonus_per_vote
        # Symmetric clamp: never let feedback dominate the semantic signal
        return max(-self._max_bonus, min(self._max_bonus, raw))

    def get_feedback_score(self, file_id: str, vault_id: Optional[int]) -> FeedbackScore | None:
        """Return the FeedbackScore for a single file_id, or None when no feedback exists."""
        self._ensure_cache_populated(vault_id)
        cache_key: Optional[int] = None if not self._vault_scope else vault_id
        return self._cache.get(cache_key, {}).get(file_id)

    # ------------------------------------------------------------------
    # Reranking API
    # ------------------------------------------------------------------

    def rerank(
        self,
        chunks: List[object],
        vault_id: Optional[int],
    ) -> List[object]:
        """Adjust chunk scores with feedback bonuses and re-sort in-place.

        This method is designed to work with any chunk-like object that has
        ``file_id`` (str) and ``score`` (float) attributes or dict-key access.

        Documents with a positive net feedback count are boosted (score += bonus);
        documents with a negative net feedback count are penalised (score -= |bonus|);
        documents with zero or no feedback are left unchanged.

        The sort is descending by the adjusted score so high-feedback documents
        rise to the top.

        Parameters
        ----------
        chunks : List[object]
            List of retrieved chunks. Each chunk must expose ``file_id`` (str) and
            ``score`` (float) via attribute access or dict-key access.
        vault_id : int | None
            Vault scope for the feedback query.

        Returns
        -------
        List[object]
            The same chunk list, re-sorted by feedback-adjusted scores.
        """
        if not chunks:
            return chunks

        self._ensure_cache_populated(vault_id)

        # First pass: write adjusted score back onto each chunk so the sort
        # order is reflected in the chunk objects themselves.
        for chunk in chunks:
            file_id: str
            if isinstance(chunk, dict):
                file_id = str(chunk.get("file_id") or "")
            else:
                file_id = str(getattr(chunk, "file_id", "") or "")
            base = (
                float(chunk["score"])
                if isinstance(chunk, dict)
                else float(getattr(chunk, "score", 0.0) or 0.0)
            )
            bonus = self._feedback_bonus(file_id, vault_id)
            adjusted = base + bonus
            if isinstance(chunk, dict):
                chunk["score"] = adjusted
            else:
                chunk.score = adjusted

        # Second pass: sort by the now-written adjusted scores.
        return sorted(chunks, key=lambda c: c["score"] if isinstance(c, dict) else c.score, reverse=True)

    async def rerank_async(
        self,
        chunks: List[object],
        vault_id: Optional[int],
    ) -> List[object]:
        """Async wrapper around :meth:`rerank` — runs the sync rerank in a thread pool."""
        return await asyncio.to_thread(self.rerank, chunks, vault_id)
