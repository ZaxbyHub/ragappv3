"""Wiki retrieval service for RAG-first evidence lookup.

Queries wiki entities, relations, claims, and pages from the Knowledge
Compiler database and returns ranked WikiEvidence records for injection
into the RAG prompt as [W#] citations.

Key design decisions:
- Entity/acronym matching is deterministic (exact + json_each alias lookup),
  not LLM-semantic, to prevent cross-entity bleed (AFOMIS vs AFMEDCOM).
- Predicate/role intent is extracted from the query to prefer e.g. "chief"
  over "deputy" when the query asks for the chief.
- FTS queries are normalized: stop words stripped, acronyms preserved,
  FTS5 operators sanitized.
- vault_id=None returns [] immediately (no cross-vault leakage).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stop words to strip from FTS queries
# ---------------------------------------------------------------------------
_STOP_WORDS = frozenset(
    {
        "who", "what", "when", "where", "why", "how",
        "is", "are", "was", "were", "be", "been", "being",
        "the", "a", "an", "of", "for", "to", "in", "on", "about",
        "at", "by", "with", "from", "as", "or", "and", "but", "not",
        "tell", "me", "give", "please", "can", "could", "would",
        "do", "does", "did", "has", "have", "had",
    }
)

# FTS5 special characters to escape/strip
_FTS5_SPECIAL = re.compile(r'["\*()\-/:;,?!@#$%^&+=<>{}|\[\]\\]')

# Common role/predicate keywords to extract predicate intent
_PREDICATE_TERMS = {
    "chief", "deputy", "director", "head", "manager", "commander",
    "officer", "lead", "president", "chair", "chairman", "secretary",
    "administrator", "coordinator", "supervisor", "boss", "owner",
    "founder", "ceo", "cto", "cfo", "coo",
}

# Pattern: acronyms are 2+ uppercase letters
_ACRONYM_RE = re.compile(r"\b([A-Z]{2,})\b")

# FTS5 boolean operators to strip even when they match the acronym pattern
_FTS5_KEYWORDS = frozenset({"AND", "OR", "NOT", "NEAR"})

# Pattern: question-subject (who is the X chief? → X=entity, chief=predicate)
_QUESTION_ENTITY_RE = re.compile(
    r"(?:who|what)\s+is\s+(?:the\s+)?([A-Za-z0-9]{2,}(?:\s+[A-Za-z0-9]+)*?)(?:\s+" + "|\\s+".join(_PREDICATE_TERMS) + r")?\??$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# WikiEvidence DTO
# ---------------------------------------------------------------------------

@dataclass
class WikiEvidence:
    """Evidence record returned by WikiRetrievalService.retrieve()."""

    label_placeholder: str  # "W1", "W2", etc. — assigned after ranking
    page_id: int
    claim_id: Optional[int]
    title: str
    slug: str
    page_type: str
    claim_text: Optional[str]
    excerpt: str
    confidence: float
    page_status: str
    claim_status: Optional[str]
    score: float
    score_type: str  # exact_entity | relation | claim_fts | page_fts | hybrid
    freshness: Optional[str]
    source_count: int
    provenance_summary: str
    matched_entity: Optional[str] = None
    matched_predicate: Optional[str] = None
    filtered_reason: Optional[str] = None
    # Internal fields for entity mismatch filtering (issue #102).
    # Populated by _fts_claim_search; not exposed in to_dict().
    _claim_subject: Optional[str] = None
    _claim_object: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": f"w_{self.page_id}_{self.claim_id or 'page'}",
            "wiki_label": self.label_placeholder,
            "page_id": self.page_id,
            "claim_id": self.claim_id,
            "title": self.title,
            "slug": self.slug,
            "page_type": self.page_type,
            "claim_text": self.claim_text,
            "confidence": self.confidence,
            "page_status": self.page_status,
            "claim_status": self.claim_status,
            "status": self.claim_status or self.page_status,
            "source_count": self.source_count,
            "provenance_summary": self.provenance_summary,
            "score": self.score,
            "score_type": self.score_type,
            "freshness": self.freshness,
            "matched_entity": self.matched_entity,
            "matched_predicate": self.matched_predicate,
        }


# ---------------------------------------------------------------------------
# Query normalizer
# ---------------------------------------------------------------------------

def normalize_fts_query(query: str) -> str:
    """Normalize a natural-language query for FTS5 MATCH.

    - Preserves acronyms (ALL-CAPS sequences).
    - Strips stop words.
    - Strips FTS5 operator characters.
    - Returns space-joined meaningful tokens, or empty string if none remain.
    """
    # Preserve acronyms before lowercasing
    tokens = query.split()
    result = []
    for tok in tokens:
        # Strip FTS5 special chars from token
        clean = _FTS5_SPECIAL.sub(" ", tok).strip()
        if not clean:
            continue
        # Skip FTS5 boolean operators even in uppercase
        if clean.upper() in _FTS5_KEYWORDS:
            continue
        # Keep acronyms as-is (2+ uppercase)
        if _ACRONYM_RE.fullmatch(clean):
            result.append(clean)
            continue
        lower = clean.lower()
        if lower not in _STOP_WORDS and len(lower) >= 2:
            result.append(lower)
    return " ".join(result)


def extract_query_intent(query: str) -> tuple[list[str], list[str]]:
    """Extract entity candidates and predicate/role terms from a query.

    Returns (entity_candidates, predicate_terms).
    entity_candidates: acronyms + question-subject patterns (case-folded for matching)
    predicate_terms: role keywords found in query
    """
    entities: list[str] = []
    predicates: list[str] = []

    # Extract ALL-CAPS acronyms
    for m in _ACRONYM_RE.finditer(query):
        acr = m.group(1)
        if acr not in entities:
            entities.append(acr)

    # Extract predicate/role terms
    lower_query = query.lower()
    for term in _PREDICATE_TERMS:
        if re.search(r"\b" + re.escape(term) + r"\b", lower_query):
            predicates.append(term)

    return entities, predicates


# ---------------------------------------------------------------------------
# WikiRetrievalService
# ---------------------------------------------------------------------------

class WikiRetrievalService:
    """Retrieves wiki evidence for RAG queries.

    Takes a connection pool (or a raw sqlite3.Connection for testing).
    For production use, pass the app's db_pool; the retrieve() method
    borrows a connection, runs queries, and returns the connection.
    """

    def __init__(
        self,
        pool: Any,
        fts_page_search_max_candidates: Optional[int] = None,
    ) -> None:
        self._pool = pool
        # Configurable FTS page-search fallback threshold (issue #101).
        # Resolved lazily so tests and ad-hoc construction don't pay the
        # Settings import cost; production callers in lifespan.py get the
        # live value from app.config.settings (env-overridable via
        # WIKI_FTS_PAGE_SEARCH_MAX_CANDIDATES).
        if fts_page_search_max_candidates is None:
            from app.config import settings

            fts_page_search_max_candidates = (
                settings.wiki_fts_page_search_max_candidates
            )
        self._fts_page_search_max_candidates = max(0, fts_page_search_max_candidates)

    def _acquire(self) -> sqlite3.Connection:
        # Support both the production SQLiteConnectionPool
        # (get_connection/release_connection) and lightweight test pools
        # (get/put).
        if hasattr(self._pool, "get_connection"):
            return self._pool.get_connection()
        return self._pool.get()

    def _release(self, conn: sqlite3.Connection) -> None:
        if hasattr(self._pool, "release_connection"):
            self._pool.release_connection(conn)
        else:
            self._pool.put(conn)

    def retrieve(self, query: str, vault_id: Optional[int]) -> List[WikiEvidence]:
        """Return ranked WikiEvidence for the query.

        Returns [] immediately if vault_id is None.
        """
        if vault_id is None:
            return []

        conn = self._acquire()
        try:
            return self._retrieve_sync(conn, query, vault_id)
        except Exception as exc:
            logger.warning("Wiki retrieval failed: %s", exc, exc_info=True)
            return []
        finally:
            self._release(conn)

    def _retrieve_sync(
        self, conn: sqlite3.Connection, query: str, vault_id: int
    ) -> List[WikiEvidence]:
        entity_candidates, predicate_terms = extract_query_intent(query)
        normalized_query = normalize_fts_query(query)

        candidates: dict[str, WikiEvidence] = {}  # key: claim_id or page_id string
        filtered_log: list[str] = []

        # 1. Exact entity match
        matched_entities = self._entity_exact_match(conn, entity_candidates, vault_id)

        # Build expanded entity text set for mismatch filtering (issue #102).
        # The raw entity_candidates (e.g. "AFOMIS") may not appear as a
        # substring in every valid claim — some claims reference the entity
        # only through the relation graph.  Include canonical names and
        # aliases from matched entities so that related claims pass through.
        _expanded_entity_texts: set[str] = {e.lower() for e in entity_candidates}
        for ent in matched_entities:
            _expanded_entity_texts.add(ent.canonical_name.lower())
            for alias in ent.aliases:
                if isinstance(alias, str) and alias:
                    _expanded_entity_texts.add(alias.lower())

        # 2. Relation lookup from matched entities
        if matched_entities:
            for entity in matched_entities:
                rel_evidence = self._relation_lookup(
                    conn, entity, predicate_terms, vault_id
                )
                for ev in rel_evidence:
                    key = f"claim_{ev.claim_id}" if ev.claim_id else f"page_{ev.page_id}"
                    if key not in candidates or ev.score > candidates[key].score:
                        candidates[key] = ev

            # Also get direct entity page evidence
            for entity in matched_entities:
                if entity.page_id:
                    page_ev = self._get_page_evidence(conn, entity.page_id, vault_id, entity.canonical_name, score=0.85, score_type="exact_entity")
                    if page_ev:
                        key = f"page_{page_ev.page_id}"
                        if key not in candidates or page_ev.score > candidates[key].score:
                            candidates[key] = page_ev

        # 3. FTS claim search
        if normalized_query:
            fts_claim_results = self._fts_claim_search(conn, normalized_query, vault_id)
            for ev in fts_claim_results:
                # Entity mismatch filter: if we have explicit entity candidates,
                # reject claims where none of the expanded entity texts appear
                # in claim text.  Uses canonical names + aliases from the
                # relation graph, not just raw query tokens (issue #102).
                if _expanded_entity_texts:
                    claim_text_lower = (ev.claim_text or "").lower()
                    page_title_lower = ev.title.lower()
                    # Also check claim's structured subject/object fields
                    # (issue #102) — FTS may match on these even when the
                    # entity name is absent from claim_text.
                    claim_subject_lower = (ev._claim_subject or "").lower()
                    claim_object_lower = (ev._claim_object or "").lower()
                    combined = (
                        claim_text_lower + " " + page_title_lower
                        + " " + claim_subject_lower + " " + claim_object_lower
                    )
                    if not any(ent in combined for ent in _expanded_entity_texts):
                        ev.filtered_reason = f"entity_mismatch: {entity_candidates}"
                        filtered_log.append(f"{ev.label_placeholder}:entity_mismatch")
                        continue
                key = f"claim_{ev.claim_id}" if ev.claim_id else f"page_{ev.page_id}"
                if key not in candidates or ev.score > candidates[key].score:
                    candidates[key] = ev

        # 4. FTS page search (fallback, lower score). Threshold is
        #    configurable via wiki_fts_page_search_max_candidates (issue #101);
        #    a value of 0 forces this fallback to run for every query.
        if normalized_query and len(candidates) < self._fts_page_search_max_candidates:
            fts_page_results = self._fts_page_search(conn, normalized_query, vault_id)
            for ev in fts_page_results:
                if _expanded_entity_texts:
                    title_lower = ev.title.lower()
                    if not any(ent in title_lower for ent in _expanded_entity_texts):
                        ev.filtered_reason = f"entity_mismatch: {entity_candidates}"
                        filtered_log.append(f"{ev.label_placeholder}:entity_mismatch")
                        continue
                key = f"page_{ev.page_id}"
                if key not in candidates:
                    candidates[key] = ev

        if not candidates:
            return []

        # Sort: relation predicate match first, then exact entity, then confidence/score
        results = list(candidates.values())
        results.sort(key=lambda e: (
            -1 if (e.score_type == "relation" and bool(e.matched_predicate)) else 0,
            -1 if e.score_type == "exact_entity" else 0,
            -e.confidence,
            -e.score,
        ))

        # Assign W labels
        for i, ev in enumerate(results, 1):
            ev.label_placeholder = f"W{i}"

        return results

    # -----------------------------------------------------------------------
    # Internal query methods
    # -----------------------------------------------------------------------

    def _entity_exact_match(
        self, conn: sqlite3.Connection, candidates: list[str], vault_id: int
    ) -> list:
        """Return WikiEntity rows matching any of the candidate strings."""
        if not candidates:
            return []

        matched = []
        for name in candidates:
            # Direct canonical_name match (case-insensitive)
            rows = conn.execute(
                "SELECT * FROM wiki_entities WHERE vault_id = ? AND lower(canonical_name) = lower(?)",
                (vault_id, name),
            ).fetchall()
            for row in rows:
                entity = _row_to_entity(row)
                if entity not in matched:
                    matched.append(entity)

            # Alias match via json_each (not LIKE — prevents false substring matches)
            alias_rows = conn.execute(
                """SELECT e.* FROM wiki_entities e,
                   json_each(e.aliases_json) AS alias
                   WHERE e.vault_id = ?
                   AND lower(alias.value) = lower(?)""",
                (vault_id, name),
            ).fetchall()
            for row in alias_rows:
                entity = _row_to_entity(row)
                if entity not in matched:
                    matched.append(entity)

        return matched

    def _relation_lookup(
        self,
        conn: sqlite3.Connection,
        entity: Any,
        predicate_terms: list[str],
        vault_id: int,
    ) -> List[WikiEvidence]:
        """Look up relations from an entity, optionally filtered by predicate."""
        rows = conn.execute(
            """SELECT r.*, c.claim_text, c.status AS claim_status, c.confidence AS claim_confidence,
                      p.title, p.slug, p.page_type, p.status AS page_status, p.last_compiled_at,
                      p.summary
               FROM wiki_relations r
               LEFT JOIN wiki_claims c ON r.claim_id = c.id
               LEFT JOIN wiki_pages p ON c.page_id = p.id
               WHERE r.vault_id = ? AND r.subject_entity_id = ?
               AND (c.status IS NULL OR c.status = 'active')
               ORDER BY r.confidence DESC""",
            (vault_id, entity.id),
        ).fetchall()

        results = []
        claim_ids = [d_claim_id for row in rows if (d_claim_id := dict(row).get("claim_id"))]
        provenance_by_claim = self._claim_provenance_for(conn, claim_ids)
        for row in rows:
            d = dict(row)
            predicate = (d.get("predicate") or "").lower()

            # Score based on predicate match
            base_score = float(d.get("claim_confidence") or d.get("confidence") or 0.8)
            matched_pred = None
            if predicate_terms:
                if any(pt in predicate for pt in predicate_terms):
                    score = base_score + 0.1
                    matched_pred = predicate
                else:
                    # Penalize non-matching predicates when query specifies a role
                    score = base_score - 0.15
            else:
                score = base_score

            source_count, provenance = provenance_by_claim.get(d.get("claim_id"), (0, ""))
            freshness = d.get("last_compiled_at")

            ev = WikiEvidence(
                label_placeholder="W?",
                page_id=d.get("page_id") or 0,
                claim_id=d.get("claim_id"),
                title=d.get("title") or entity.canonical_name,
                slug=d.get("slug") or "",
                page_type=d.get("page_type") or "entity",
                claim_text=d.get("claim_text"),
                excerpt=d.get("summary") or d.get("claim_text") or "",
                confidence=float(d.get("claim_confidence") or 0.8),
                page_status=d.get("page_status") or "draft",
                claim_status=d.get("claim_status"),
                score=max(0.0, score),
                score_type="relation",
                freshness=freshness,
                source_count=source_count,
                provenance_summary=provenance,
                matched_entity=entity.canonical_name,
                matched_predicate=matched_pred,
            )
            results.append(ev)

        return results

    def _get_page_evidence(
        self,
        conn: sqlite3.Connection,
        page_id: int,
        vault_id: int,
        entity_name: str,
        score: float,
        score_type: str,
    ) -> Optional[WikiEvidence]:
        row = conn.execute(
            "SELECT * FROM wiki_pages WHERE id = ? AND vault_id = ?",
            (page_id, vault_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        return WikiEvidence(
            label_placeholder="W?",
            page_id=page_id,
            claim_id=None,
            title=d["title"],
            slug=d["slug"],
            page_type=d["page_type"],
            claim_text=None,
            excerpt=d.get("summary") or "",
            confidence=float(d.get("confidence") or 0.8),
            page_status=d.get("status") or "draft",
            claim_status=None,
            score=score,
            score_type=score_type,
            freshness=d.get("last_compiled_at"),
            source_count=0,
            provenance_summary="entity page",
            matched_entity=entity_name,
        )

    def _fts_claim_search(
        self, conn: sqlite3.Connection, normalized_query: str, vault_id: int
    ) -> List[WikiEvidence]:
        if not normalized_query:
            return []
        try:
            rows = conn.execute(
                """SELECT c.*, p.title, p.slug, p.page_type, p.status AS page_status,
                          p.summary, p.last_compiled_at
                   FROM wiki_claims_fts
                   JOIN wiki_claims c ON wiki_claims_fts.rowid = c.id
                   LEFT JOIN wiki_pages p ON c.page_id = p.id
                   WHERE wiki_claims_fts MATCH ? AND c.vault_id = ?
                   AND (c.status IS NULL OR c.status = 'active')
                   ORDER BY rank
                   LIMIT 10""",
                (normalized_query, vault_id),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("Wiki FTS claim search error (query=%r): %s", normalized_query, exc)
            return []

        results = []
        provenance_by_claim = self._claim_provenance_for(conn, [dict(r)["id"] for r in rows])
        for i, row in enumerate(rows):
            d = dict(row)
            source_count, provenance = provenance_by_claim.get(d["id"], (0, ""))
            results.append(WikiEvidence(
                label_placeholder="W?",
                page_id=d.get("page_id") or 0,
                claim_id=d["id"],
                title=d.get("title") or "",
                slug=d.get("slug") or "",
                page_type=d.get("page_type") or "entity",
                claim_text=d.get("claim_text"),
                excerpt=d.get("summary") or d.get("claim_text") or "",
                confidence=float(d.get("confidence") or 0.7),
                page_status=d.get("page_status") or "draft",
                claim_status=d.get("status"),
                score=max(0.1, 0.75 - i * 0.05),
                score_type="claim_fts",
                freshness=d.get("last_compiled_at"),
                source_count=source_count,
                provenance_summary=provenance,
                _claim_subject=d.get("subject"),
                _claim_object=d.get("object"),
            ))
        return results

    def _fts_page_search(
        self, conn: sqlite3.Connection, normalized_query: str, vault_id: int
    ) -> List[WikiEvidence]:
        if not normalized_query:
            return []
        try:
            rows = conn.execute(
                """SELECT p.*
                   FROM wiki_pages_fts
                   JOIN wiki_pages p ON wiki_pages_fts.rowid = p.id
                   WHERE wiki_pages_fts MATCH ? AND p.vault_id = ?
                   ORDER BY rank
                   LIMIT 5""",
                (normalized_query, vault_id),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("Wiki FTS page search error (query=%r): %s", normalized_query, exc)
            return []

        results = []
        for i, row in enumerate(rows):
            d = dict(row)
            results.append(WikiEvidence(
                label_placeholder="W?",
                page_id=d["id"],
                claim_id=None,
                title=d["title"],
                slug=d["slug"],
                page_type=d.get("page_type") or "overview",
                claim_text=None,
                excerpt=d.get("summary") or "",
                confidence=float(d.get("confidence") or 0.6),
                page_status=d.get("status") or "draft",
                claim_status=None,
                score=max(0.1, 0.6 - i * 0.05),
                score_type="page_fts",
                freshness=d.get("last_compiled_at"),
                source_count=0,
                provenance_summary="wiki page",
            ))
        return results

    def _claim_provenance_for(
        self, conn: sqlite3.Connection, claim_ids: list[int]
    ) -> dict[int, tuple[int, str]]:
        """Batched provenance lookup (issue #276 E1-3).

        Issues a single ``WHERE claim_id IN (...)`` query and groups results in
        Python, replacing the per-row single-row query that fired
        O(relation_rows + fts_claim_rows) times per retrieval.
        Returns ``{claim_id: (source_count, provenance_summary)}``.
        """
        if not claim_ids:
            return {}
        placeholders = ",".join("?" for _ in claim_ids)
        try:
            rows = conn.execute(
                f"SELECT claim_id, source_kind FROM wiki_claim_sources "
                f"WHERE claim_id IN ({placeholders})",
                tuple(claim_ids),
            ).fetchall()
        except Exception as exc:
            logger.warning(
                "_claim_provenance_for: failed for %d claim(s): %s",
                len(claim_ids),
                exc,
            )
            return {cid: (0, "") for cid in claim_ids}

        grouped: dict[int, list[str]] = {}
        for r in rows:
            claim_id, source_kind = r[0], r[1]
            grouped.setdefault(claim_id, []).append(source_kind)

        result: dict[int, tuple[int, str]] = {}
        for cid in claim_ids:
            kinds = grouped.get(cid)
            if not kinds:
                result[cid] = (0, "")
                continue
            summary_parts = []
            doc_count = kinds.count("document")
            mem_count = kinds.count("memory")
            if doc_count:
                summary_parts.append(f"{doc_count} doc{'s' if doc_count > 1 else ''}")
            if mem_count:
                summary_parts.append(f"{mem_count} memory")
            result[cid] = (len(kinds), ", ".join(summary_parts) or "manual")
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_entity(row: sqlite3.Row) -> Any:
    """Convert a sqlite3.Row from wiki_entities into a simple namespace."""
    d = dict(row)

    class _Entity:
        def __init__(self, data: dict) -> None:
            self.__dict__.update(data)
            self.aliases: list = []
            try:
                self.aliases = json.loads(data.get("aliases_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                pass

        def __eq__(self, other: Any) -> bool:
            return isinstance(other, _Entity) and self.id == other.id

    return _Entity(d)


__all__ = ["WikiEvidence", "WikiRetrievalService", "normalize_fts_query", "extract_query_intent"]
