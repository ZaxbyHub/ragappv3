"""KMS retrieval service for RAG-first evidence lookup.

Queries user-curated ``kms_entries`` via the ``kms_entries_fts`` full-text index
and returns ranked ``KMSEvidence`` records for injection into the RAG prompt as
``[K#]`` citations — the user-curated counterpart to the AI-extracted ``[W#]``
wiki evidence.

Key design decisions:
- Vault-scoped: ``vault_id=None`` returns ``[]`` immediately (no cross-vault leak).
- Gated by ``settings.kms_enabled`` so the master switch also turns off retrieval.
- Only ``draft`` and ``published`` entries are retrieved; ``archived`` entries are
  explicitly excluded. (Document-sourced entries default to ``draft``, so drafts
  must be eligible for the on-ingest pipeline to surface anything by default.)
- FTS5 operators are sanitized to prefix-matched alnum tokens to avoid
  ``OperationalError`` on punctuation-heavy natural-language queries.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Leading characters of the body used as the prompt/card excerpt.
_EXCERPT_CHARS = 600
# Maximum FTS candidates pulled per query.
_FTS_LIMIT = 8


def build_kms_fts_query(raw_search: str) -> str:
    """Sanitize a natural-language query into a safe FTS5 prefix-match string.

    Strips hyphens (FTS5 column-filter syntax) and punctuation, keeps alnum/_
    tokens, and prefix-matches each token. Returns "" when no usable token
    remains (caller then skips the search).
    """
    normalized = (raw_search or "").lower().replace("-", " ")
    tokens = re.findall(r"[a-z0-9_]+", normalized)
    return " ".join(f"{token}*" for token in tokens[:8])


@dataclass
class KMSEvidence:
    """Evidence record returned by KMSRetrievalService.retrieve()."""

    label_placeholder: str  # "K1", "K2", … — assigned after ranking
    entry_id: int
    slug: str
    title: str
    summary: str
    excerpt: str
    tags: List[str]
    status: str
    source_type: str
    file_id: Optional[int]
    score: float
    score_type: str  # kms_fts

    def to_dict(self) -> dict:
        return {
            "id": f"k_{self.entry_id}",
            "kms_label": self.label_placeholder,
            "entry_id": self.entry_id,
            "slug": self.slug,
            "title": self.title,
            "summary": self.summary,
            "excerpt": self.excerpt,
            "tags": self.tags,
            "status": self.status,
            "source_type": self.source_type,
            "file_id": self.file_id,
            "score": self.score,
            "score_type": self.score_type,
        }


class KMSRetrievalService:
    """Retrieves KMS evidence for RAG queries.

    Takes a connection pool. Supports both pool interfaces in this codebase:
    the production ``SQLiteConnectionPool`` (``get_connection``/
    ``release_connection``) and the lightweight test pools (``get``/``put``).
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def _acquire(self) -> sqlite3.Connection:
        if hasattr(self._pool, "get_connection"):
            return self._pool.get_connection()
        return self._pool.get()

    def _release(self, conn: sqlite3.Connection) -> None:
        if hasattr(self._pool, "release_connection"):
            self._pool.release_connection(conn)
        else:
            self._pool.put(conn)

    def retrieve(self, query: str, vault_id: Optional[int]) -> List[KMSEvidence]:
        """Return ranked KMSEvidence for the query.

        Returns ``[]`` immediately when ``vault_id`` is None or KMS is disabled.
        """
        if vault_id is None or not settings.kms_enabled:
            return []

        fts_query = build_kms_fts_query(query)
        if not fts_query:
            return []

        conn = self._acquire()
        try:
            return self._retrieve_sync(conn, fts_query, vault_id)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("KMS retrieval failed: %s", exc, exc_info=True)
            return []
        finally:
            self._release(conn)

    def _retrieve_sync(
        self, conn: sqlite3.Connection, fts_query: str, vault_id: int
    ) -> List[KMSEvidence]:
        try:
            rows = conn.execute(
                """
                SELECT e.id, e.slug, e.title, e.summary, e.body, e.tags_json,
                       e.status, e.source_type, e.file_id
                FROM kms_entries_fts
                JOIN kms_entries e ON kms_entries_fts.rowid = e.id
                WHERE kms_entries_fts MATCH ?
                  AND e.vault_id = ?
                  AND e.status IN ('draft', 'published')
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, vault_id, _FTS_LIMIT),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("KMS FTS search error (query=%r): %s", fts_query, exc)
            return []

        results: List[KMSEvidence] = []
        for i, row in enumerate(rows):
            d = dict(row)
            try:
                tags = json.loads(d.get("tags_json") or "[]")
                if not isinstance(tags, list):
                    tags = []
            except (json.JSONDecodeError, TypeError):
                tags = []
            body = d.get("body") or ""
            summary = d.get("summary") or ""
            excerpt = summary or body[:_EXCERPT_CHARS]
            results.append(
                KMSEvidence(
                    label_placeholder=f"K{i + 1}",
                    entry_id=d["id"],
                    slug=d.get("slug") or "",
                    title=d.get("title") or "",
                    summary=summary,
                    excerpt=excerpt,
                    tags=tags,
                    status=d.get("status") or "draft",
                    source_type=d.get("source_type") or "manual",
                    file_id=d.get("file_id"),
                    score=max(0.1, 0.7 - i * 0.05),
                    score_type="kms_fts",
                )
            )
        return results


__all__ = ["KMSEvidence", "KMSRetrievalService", "build_kms_fts_query"]
