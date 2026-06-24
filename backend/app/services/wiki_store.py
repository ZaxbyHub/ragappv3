"""
WikiStore: CRUD operations for all wiki / Knowledge Compiler tables.

All operations are vault-scoped. Slug normalization is enforced on create/update.
FTS search is backed by wiki_pages_fts, wiki_claims_fts, wiki_entities_fts.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# DTO dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WikiPage:
    id: int
    vault_id: int
    slug: str
    title: str
    page_type: str
    markdown: str
    summary: str
    status: str
    confidence: float
    created_by: Optional[int]
    created_at: str
    updated_at: str
    last_compiled_at: Optional[str]
    parent_id: Optional[int] = None
    version: int = 1
    claims: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    lint_findings: list = field(default_factory=list)


@dataclass
class WikiClaimSource:
    id: int
    claim_id: int
    source_kind: str
    file_id: Optional[int]
    chunk_id: Optional[str]
    memory_id: Optional[int]
    chat_message_id: Optional[int]
    source_label: Optional[str]
    quote: Optional[str]
    char_start: Optional[int]
    char_end: Optional[int]
    page_number: Optional[int]
    confidence: float
    created_at: str


@dataclass
class WikiClaim:
    id: int
    vault_id: int
    page_id: Optional[int]
    claim_text: str
    claim_type: str
    subject: Optional[str]
    predicate: Optional[str]
    object: Optional[str]
    source_type: str
    status: str
    confidence: float
    created_by: Optional[int]
    # 'deterministic' | 'llm_curator' | None.
    # None for legacy rows pre-PR-C; readers should treat None as
    # "deterministic / unknown".
    created_by_kind: Optional[str]
    created_at: str
    updated_at: str
    sources: list = field(default_factory=list)


@dataclass
class WikiEntity:
    id: int
    vault_id: int
    canonical_name: str
    entity_type: str
    aliases_json: str
    description: str
    page_id: Optional[int]
    created_at: str
    updated_at: str

    @property
    def aliases(self) -> list:
        try:
            return json.loads(self.aliases_json)
        except (json.JSONDecodeError, TypeError):
            return []


@dataclass
class WikiRelation:
    id: int
    vault_id: int
    subject_entity_id: Optional[int]
    predicate: str
    object_entity_id: Optional[int]
    object_text: Optional[str]
    claim_id: Optional[int]
    confidence: float
    created_at: str


@dataclass
class WikiCompileJob:
    id: int
    vault_id: int
    trigger_type: str
    trigger_id: Optional[str]
    status: str
    error: Optional[str]
    result_json: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    input_json: Optional[str] = None
    retry_count: int = 0


@dataclass
class WikiLintFinding:
    id: int
    vault_id: int
    finding_type: str
    severity: str
    title: str
    details: str
    related_page_ids_json: str
    related_claim_ids_json: str
    status: str
    created_at: str
    updated_at: str


@dataclass
class WikiPageVersion:
    id: int
    page_id: int
    vault_id: int
    title: str
    markdown: str
    summary: str
    status: str
    confidence: float
    edited_by: Optional[int]
    created_at: str


@dataclass
class WikiPageFile:
    id: int
    page_id: int
    file_id: int
    vault_id: int
    created_at: str


@dataclass
class WikiPageLink:
    id: int
    source_page_id: int
    target_page_id: int
    vault_id: int
    link_text: Optional[str]
    created_at: str


@dataclass
class WikiActivityEntry:
    id: int
    vault_id: int
    action: str
    target_type: str
    target_id: int
    user_id: Optional[int]
    details_json: str
    created_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_slug(text: str) -> str:
    """Lowercase, strip special chars, replace whitespace/underscores with hyphens."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def normalize_claim_text(text: str) -> str:
    """Normalize claim text for fuzzy dedup: lowercase, strip punctuation,
    collapse whitespace. Stored in the indexed ``wiki_claims.normalized_text``
    column so near-duplicate claims can be found with a bounded indexed lookup
    instead of a full-table scan (DD-C011)."""
    normalized = re.sub(r"[^\w\s]", "", (text or "").lower().strip())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _to_wiki_page(row: sqlite3.Row) -> WikiPage:
    d = _row_to_dict(row)
    return WikiPage(
        id=d["id"],
        vault_id=d["vault_id"],
        slug=d["slug"],
        title=d["title"],
        page_type=d["page_type"],
        markdown=d["markdown"],
        summary=d["summary"] or "",
        status=d["status"],
        confidence=d["confidence"] or 0.0,
        created_by=d.get("created_by"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        last_compiled_at=d.get("last_compiled_at"),
        parent_id=d.get("parent_id"),
        version=d.get("version") or 1,
    )


def _to_claim_source(row: sqlite3.Row) -> WikiClaimSource:
    d = _row_to_dict(row)
    return WikiClaimSource(
        id=d["id"],
        claim_id=d["claim_id"],
        source_kind=d["source_kind"],
        file_id=d.get("file_id"),
        chunk_id=d.get("chunk_id"),
        memory_id=d.get("memory_id"),
        chat_message_id=d.get("chat_message_id"),
        source_label=d.get("source_label"),
        quote=d.get("quote"),
        char_start=d.get("char_start"),
        char_end=d.get("char_end"),
        page_number=d.get("page_number"),
        confidence=d.get("confidence") or 0.0,
        created_at=d["created_at"],
    )


def _to_wiki_claim(row: sqlite3.Row) -> WikiClaim:
    d = _row_to_dict(row)
    return WikiClaim(
        id=d["id"],
        vault_id=d["vault_id"],
        page_id=d.get("page_id"),
        claim_text=d["claim_text"],
        claim_type=d.get("claim_type", "fact"),
        subject=d.get("subject"),
        predicate=d.get("predicate"),
        object=d.get("object"),
        source_type=d["source_type"],
        status=d["status"],
        confidence=d.get("confidence") or 0.0,
        created_by=d.get("created_by"),
        created_by_kind=d.get("created_by_kind"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )


def _to_wiki_entity(row: sqlite3.Row) -> WikiEntity:
    d = _row_to_dict(row)
    return WikiEntity(
        id=d["id"],
        vault_id=d["vault_id"],
        canonical_name=d["canonical_name"],
        entity_type=d.get("entity_type", "unknown"),
        aliases_json=d.get("aliases_json") or "[]",
        description=d.get("description") or "",
        page_id=d.get("page_id"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )


def _to_wiki_relation(row: sqlite3.Row) -> WikiRelation:
    d = _row_to_dict(row)
    return WikiRelation(
        id=d["id"],
        vault_id=d["vault_id"],
        subject_entity_id=d.get("subject_entity_id"),
        predicate=d["predicate"],
        object_entity_id=d.get("object_entity_id"),
        object_text=d.get("object_text"),
        claim_id=d.get("claim_id"),
        confidence=d.get("confidence") or 0.0,
        created_at=d["created_at"],
    )


def _to_compile_job(row: sqlite3.Row) -> WikiCompileJob:
    d = _row_to_dict(row)
    return WikiCompileJob(
        id=d["id"],
        vault_id=d["vault_id"],
        trigger_type=d["trigger_type"],
        trigger_id=d.get("trigger_id"),
        status=d["status"],
        error=d.get("error"),
        result_json=d.get("result_json") or "{}",
        created_at=d["created_at"],
        started_at=d.get("started_at"),
        completed_at=d.get("completed_at"),
        input_json=d.get("input_json"),
        retry_count=d.get("retry_count") or 0,
    )


def _to_lint_finding(row: sqlite3.Row) -> WikiLintFinding:
    d = _row_to_dict(row)
    return WikiLintFinding(
        id=d["id"],
        vault_id=d["vault_id"],
        finding_type=d["finding_type"],
        severity=d["severity"],
        title=d["title"],
        details=d.get("details") or "",
        related_page_ids_json=d.get("related_page_ids_json") or "[]",
        related_claim_ids_json=d.get("related_claim_ids_json") or "[]",
        status=d["status"],
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )


def _to_page_version(row: sqlite3.Row) -> WikiPageVersion:
    d = _row_to_dict(row)
    return WikiPageVersion(
        id=d["id"],
        page_id=d["page_id"],
        vault_id=d["vault_id"],
        title=d["title"],
        markdown=d["markdown"],
        summary=d.get("summary") or "",
        status=d["status"],
        confidence=d.get("confidence") or 0.0,
        edited_by=d.get("edited_by"),
        created_at=d["created_at"],
    )


def _to_page_file(row: sqlite3.Row) -> WikiPageFile:
    d = _row_to_dict(row)
    return WikiPageFile(
        id=d["id"],
        page_id=d["page_id"],
        file_id=d["file_id"],
        vault_id=d["vault_id"],
        created_at=d["created_at"],
    )


def _to_page_link(row: sqlite3.Row) -> WikiPageLink:
    d = _row_to_dict(row)
    return WikiPageLink(
        id=d["id"],
        source_page_id=d["source_page_id"],
        target_page_id=d["target_page_id"],
        vault_id=d["vault_id"],
        link_text=d.get("link_text"),
        created_at=d["created_at"],
    )


def _to_activity_entry(row: sqlite3.Row) -> WikiActivityEntry:
    d = _row_to_dict(row)
    return WikiActivityEntry(
        id=d["id"],
        vault_id=d["vault_id"],
        action=d["action"],
        target_type=d["target_type"],
        target_id=d["target_id"],
        user_id=d.get("user_id"),
        details_json=d.get("details_json") or "{}",
        created_at=d["created_at"],
    )


# ---------------------------------------------------------------------------
# WikiStore
# ---------------------------------------------------------------------------

class WikiStore:
    """Vault-scoped CRUD and FTS search for all wiki tables."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # -----------------------------------------------------------------------
    # Pages
    # -----------------------------------------------------------------------

    def create_page(
        self,
        vault_id: int,
        title: str,
        page_type: str,
        slug: Optional[str] = None,
        markdown: str = "",
        summary: str = "",
        status: str = "draft",
        confidence: float = 0.0,
        created_by: Optional[int] = None,
        parent_id: Optional[int] = None,
    ) -> WikiPage:
        if not slug:
            slug = normalize_slug(title)
        else:
            slug = normalize_slug(slug)
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            """
            INSERT INTO wiki_pages
                (vault_id, slug, title, page_type, markdown, summary, status, confidence, created_by, parent_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (vault_id, slug, title, page_type, markdown, summary, status, confidence, created_by, parent_id, now, now),
        )
        self._db.commit()
        page = self.get_page(cur.lastrowid)  # type: ignore[arg-type]
        if page:
            self.log_activity(vault_id, "page_created", "page", page.id, user_id=created_by)
            # DD-C030: resolve [[slug]] wiki-links in the new page's body so
            # backlinks exist as soon as the page is created.
            if markdown:
                self.sync_page_links(page.id, vault_id, markdown)
        return page  # type: ignore[return-value]

    def get_page(self, page_id: int, load_relations: bool = True) -> Optional[WikiPage]:
        row = self._db.execute(
            "SELECT * FROM wiki_pages WHERE id = ?", (page_id,)
        ).fetchone()
        if not row:
            return None
        page = _to_wiki_page(row)
        if load_relations:
            page.claims = self.list_claims(page.vault_id, page_id=page_id)
            page.entities = self.list_entities(page.vault_id, page_id=page_id)
            page.lint_findings = self.list_lint_findings(page.vault_id, page_id=page_id)
        return page

    def list_pages(
        self,
        vault_id: int,
        page_type: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[WikiPage]:
        offset = (page - 1) * per_page
        if search:
            ids = self._fts_page_ids(vault_id, search)
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            sql = f"SELECT * FROM wiki_pages WHERE id IN ({placeholders}) AND vault_id = ?"
            params: list[Any] = [*ids, vault_id]
            if page_type:
                sql += " AND page_type = ?"
                params.append(page_type)
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params += [per_page, offset]
            rows = self._db.execute(sql, params).fetchall()
        else:
            params = [vault_id]
            sql = "SELECT * FROM wiki_pages WHERE vault_id = ?"
            if page_type:
                sql += " AND page_type = ?"
                params.append(page_type)
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params += [per_page, offset]
            rows = self._db.execute(sql, params).fetchall()
        return [_to_wiki_page(r) for r in rows]

    def update_page(
        self,
        page_id: int,
        vault_id: int,
        expected_version: Optional[int] = None,
        edited_by: Optional[int] = None,
        commit: bool = True,
        **kwargs: Any,
    ) -> Optional[WikiPage]:
        allowed = {"title", "page_type", "markdown", "summary", "status", "confidence", "slug", "last_compiled_at", "parent_id"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_page(page_id)

        # DD-C020: optimistic locking. Read current version once and reuse it for
        # both the conflict check and the bump so there is a single read point.
        row = self._db.execute(
            "SELECT version FROM wiki_pages WHERE id = ? AND vault_id = ?",
            (page_id, vault_id),
        ).fetchone()
        if not row:
            return None
        current_version = dict(row)["version"]
        if expected_version is not None and current_version != expected_version:
            raise ValueError(
                f"Version conflict: expected {expected_version}, got {current_version}"
            )

        # DD-C003: snapshot current state before updating
        self.save_version(page_id, vault_id, edited_by=edited_by, commit=False)

        if "title" in updates and "slug" not in updates:
            updates["slug"] = normalize_slug(updates["title"])
        if "slug" in updates:
            updates["slug"] = normalize_slug(updates["slug"])
        updates["updated_at"] = datetime.utcnow().isoformat()
        updates["version"] = current_version + 1
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        # F-005: pin the UPDATE to the version we read so a concurrent writer that
        # already bumped the row cannot be silently clobbered (TOCTOU). The
        # rowcount check turns a lost update into an explicit conflict.
        values = list(updates.values()) + [page_id, vault_id, current_version]
        cur = self._db.execute(
            f"UPDATE wiki_pages SET {set_clause} WHERE id = ? AND vault_id = ? AND version = ?",
            values,
        )
        if cur.rowcount == 0:
            self._db.rollback()
            raise ValueError(
                f"Version conflict: page {page_id} was modified concurrently"
            )
        self.log_activity(vault_id, "page_updated", "page", page_id, user_id=edited_by, commit=False)
        # DD-C030: if the body changed, re-resolve [[slug]] wiki-links inside this
        # same transaction so the save stays atomic.
        if "markdown" in updates:
            self.sync_page_links(page_id, vault_id, updates["markdown"], commit=False)
        if commit:
            self._db.commit()
        return self.get_page(page_id)

    def delete_page(self, page_id: int, vault_id: int, commit: bool = True) -> bool:
        cur = self._db.execute(
            "DELETE FROM wiki_pages WHERE id = ? AND vault_id = ?", (page_id, vault_id)
        )
        deleted = cur.rowcount > 0
        if deleted:
            self.log_activity(vault_id, "page_deleted", "page", page_id, commit=False)
        if commit:
            self._db.commit()
        return deleted

    def _fts_page_ids(self, vault_id: int, query: str) -> list[int]:
        rows = self._db.execute(
            "SELECT rowid FROM wiki_pages_fts WHERE wiki_pages_fts MATCH ?", (query,)
        ).fetchall()
        return [r[0] for r in rows]

    # -----------------------------------------------------------------------
    # Entities
    # -----------------------------------------------------------------------

    def upsert_entity(
        self,
        vault_id: int,
        canonical_name: str,
        entity_type: str = "unknown",
        aliases: Optional[list] = None,
        description: str = "",
        page_id: Optional[int] = None,
    ) -> WikiEntity:
        now = datetime.utcnow().isoformat()
        aliases_json = json.dumps(aliases or [])
        existing = self._db.execute(
            "SELECT * FROM wiki_entities WHERE vault_id = ? AND canonical_name = ?",
            (vault_id, canonical_name),
        ).fetchone()
        if existing:
            existing_entity = _to_wiki_entity(existing)
            merged_aliases = list(set(existing_entity.aliases + (aliases or [])))
            # Guard: skip aliases that collide with another entity's canonical_name
            if merged_aliases:
                placeholders = ",".join("?" for _ in merged_aliases)
                conflicts = self._db.execute(
                    f"SELECT canonical_name FROM wiki_entities WHERE vault_id = ? AND id != ? AND lower(canonical_name) IN ({placeholders})",
                    [vault_id, existing_entity.id] + [a.lower() for a in merged_aliases],
                ).fetchall()
                if conflicts:
                    conflict_names = {r[0].lower() for r in conflicts}
                    merged_aliases = [a for a in merged_aliases if a.lower() not in conflict_names]
            merged_json = json.dumps(merged_aliases)
            new_page_id = page_id if page_id is not None else existing_entity.page_id
            new_desc = description or existing_entity.description
            self._db.execute(
                """UPDATE wiki_entities SET aliases_json = ?, description = ?, page_id = ?,
                   entity_type = ?, updated_at = ? WHERE id = ?""",
                (merged_json, new_desc, new_page_id, entity_type, now, existing_entity.id),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM wiki_entities WHERE id = ?", (existing_entity.id,)).fetchone()
        else:
            cur = self._db.execute(
                """INSERT INTO wiki_entities
                   (vault_id, canonical_name, entity_type, aliases_json, description, page_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (vault_id, canonical_name, entity_type, aliases_json, description, page_id, now, now),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM wiki_entities WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _to_wiki_entity(row)

    def get_entity(self, entity_id: int) -> Optional[WikiEntity]:
        row = self._db.execute("SELECT * FROM wiki_entities WHERE id = ?", (entity_id,)).fetchone()
        return _to_wiki_entity(row) if row else None

    def list_entities(
        self,
        vault_id: int,
        search: Optional[str] = None,
        page_id: Optional[int] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[WikiEntity]:
        # F-012: bound result sets so a large vault cannot return an unbounded list.
        page_clause = ""
        page_params: list[Any] = []
        if limit is not None:
            page_clause = " LIMIT ? OFFSET ?"
            page_params = [limit, offset]
        if search:
            ids = self._fts_entity_ids(vault_id, search)
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            rows = self._db.execute(
                f"SELECT * FROM wiki_entities WHERE id IN ({placeholders}) AND vault_id = ?"
                f" ORDER BY canonical_name{page_clause}",
                [*ids, vault_id, *page_params],
            ).fetchall()
        elif page_id is not None:
            rows = self._db.execute(
                f"SELECT * FROM wiki_entities WHERE vault_id = ? AND page_id = ?"
                f" ORDER BY canonical_name{page_clause}",
                [vault_id, page_id, *page_params],
            ).fetchall()
        else:
            rows = self._db.execute(
                f"SELECT * FROM wiki_entities WHERE vault_id = ?"
                f" ORDER BY canonical_name{page_clause}",
                [vault_id, *page_params],
            ).fetchall()
        return [_to_wiki_entity(r) for r in rows]

    def _fts_entity_ids(self, vault_id: int, query: str) -> list[int]:
        rows = self._db.execute(
            "SELECT rowid FROM wiki_entities_fts WHERE wiki_entities_fts MATCH ?", (query,)
        ).fetchall()
        return [r[0] for r in rows]

    # -----------------------------------------------------------------------
    # Claims
    # -----------------------------------------------------------------------

    def create_claim(
        self,
        vault_id: int,
        claim_text: str,
        source_type: str,
        page_id: Optional[int] = None,
        claim_type: str = "fact",
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        object: Optional[str] = None,
        status: str = "active",
        confidence: float = 0.0,
        created_by: Optional[int] = None,
        created_by_kind: Optional[str] = None,
        sources: Optional[list] = None,
    ) -> WikiClaim:
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            """INSERT INTO wiki_claims
               (vault_id, page_id, claim_text, normalized_text, claim_type, subject, predicate, object,
                source_type, status, confidence, created_by, created_by_kind,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vault_id, page_id, claim_text, normalize_claim_text(claim_text), claim_type,
             subject, predicate, object,
             source_type, status, confidence, created_by, created_by_kind, now, now),
        )
        claim_id = cur.lastrowid
        if sources:
            for src in sources:
                self._attach_source(claim_id, src)  # type: ignore[arg-type]
        self._db.commit()
        return self.get_claim(claim_id)  # type: ignore[return-value]

    def get_claim(self, claim_id: int) -> Optional[WikiClaim]:
        row = self._db.execute("SELECT * FROM wiki_claims WHERE id = ?", (claim_id,)).fetchone()
        if not row:
            return None
        claim = _to_wiki_claim(row)
        claim.sources = self._load_sources(claim_id)
        return claim

    def list_claims(
        self,
        vault_id: int,
        page_id: Optional[int] = None,
        entity: Optional[str] = None,
        search: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[WikiClaim]:
        if search:
            ids = self._fts_claim_ids(vault_id, search)
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            sql = f"SELECT * FROM wiki_claims WHERE id IN ({placeholders}) AND vault_id = ?"
            params: list[Any] = [*ids, vault_id]
        else:
            sql = "SELECT * FROM wiki_claims WHERE vault_id = ?"
            params = [vault_id]
        if page_id is not None:
            sql += " AND page_id = ?"
            params.append(page_id)
        if entity:
            sql += " AND (subject = ? OR object = ?)"
            params += [entity, entity]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        # F-012: bound result sets so a large vault cannot return an unbounded list.
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        rows = self._db.execute(sql, params).fetchall()
        claims = [_to_wiki_claim(r) for r in rows]
        # F-008: batch-load every claim's sources in a single query instead of
        # firing one SELECT per claim (N+1).
        sources_by_claim = self._load_sources_for([c.id for c in claims])
        for claim in claims:
            claim.sources = sources_by_claim.get(claim.id, [])
        return claims

    def find_claim_by_text(self, vault_id: int, claim_text: str) -> Optional[WikiClaim]:
        """Return an existing claim matching vault + claim_text, or None."""
        row = self._db.execute(
            "SELECT * FROM wiki_claims WHERE vault_id = ? AND claim_text = ? LIMIT 1",
            (vault_id, claim_text),
        ).fetchone()
        if not row:
            return None
        claim = _to_wiki_claim(row)
        claim.sources = self._load_sources(claim.id)
        return claim

    def update_claim(self, claim_id: int, vault_id: int, **kwargs: Any) -> Optional[WikiClaim]:
        allowed = {"claim_text", "claim_type", "subject", "predicate", "object", "source_type", "status", "confidence", "page_id"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_claim(claim_id)
        if "claim_text" in updates:
            updates["normalized_text"] = normalize_claim_text(updates["claim_text"])
        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [claim_id, vault_id]
        self._db.execute(
            f"UPDATE wiki_claims SET {set_clause} WHERE id = ? AND vault_id = ?", values
        )
        self._db.commit()
        return self.get_claim(claim_id)

    def delete_claim(self, claim_id: int, vault_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM wiki_claims WHERE id = ? AND vault_id = ?", (claim_id, vault_id)
        )
        self._db.commit()
        return cur.rowcount > 0

    def attach_source(
        self,
        claim_id: int,
        source_kind: str,
        file_id: Optional[int] = None,
        chunk_id: Optional[str] = None,
        memory_id: Optional[int] = None,
        chat_message_id: Optional[int] = None,
        source_label: Optional[str] = None,
        quote: Optional[str] = None,
        char_start: Optional[int] = None,
        char_end: Optional[int] = None,
        page_number: Optional[int] = None,
        confidence: float = 0.0,
    ) -> WikiClaimSource:
        src = {
            "source_kind": source_kind,
            "file_id": file_id,
            "chunk_id": chunk_id,
            "memory_id": memory_id,
            "chat_message_id": chat_message_id,
            "source_label": source_label,
            "quote": quote,
            "char_start": char_start,
            "char_end": char_end,
            "page_number": page_number,
            "confidence": confidence,
        }
        return self._attach_source(claim_id, src)

    def _attach_source(self, claim_id: int, src: dict) -> WikiClaimSource:
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            """INSERT INTO wiki_claim_sources
               (claim_id, source_kind, file_id, chunk_id, memory_id, chat_message_id,
                source_label, quote, char_start, char_end, page_number, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (claim_id,
             src.get("source_kind"), src.get("file_id"), src.get("chunk_id"),
             src.get("memory_id"), src.get("chat_message_id"), src.get("source_label"),
             src.get("quote"), src.get("char_start"), src.get("char_end"),
             src.get("page_number"), src.get("confidence", 0.0), now),
        )
        row = self._db.execute("SELECT * FROM wiki_claim_sources WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _to_claim_source(row)

    def _load_sources(self, claim_id: int) -> list[WikiClaimSource]:
        rows = self._db.execute(
            "SELECT * FROM wiki_claim_sources WHERE claim_id = ? ORDER BY id", (claim_id,)
        ).fetchall()
        return [_to_claim_source(r) for r in rows]

    def _load_sources_for(self, claim_ids: list[int]) -> dict[int, list[WikiClaimSource]]:
        """Batch-load sources for many claims in one query (avoids N+1)."""
        if not claim_ids:
            return {}
        placeholders = ",".join("?" * len(claim_ids))
        rows = self._db.execute(
            f"SELECT * FROM wiki_claim_sources WHERE claim_id IN ({placeholders}) ORDER BY claim_id, id",
            claim_ids,
        ).fetchall()
        grouped: dict[int, list[WikiClaimSource]] = {}
        for r in rows:
            src = _to_claim_source(r)
            grouped.setdefault(src.claim_id, []).append(src)
        return grouped

    def _fts_claim_ids(self, vault_id: int, query: str) -> list[int]:
        rows = self._db.execute(
            "SELECT rowid FROM wiki_claims_fts WHERE wiki_claims_fts MATCH ?", (query,)
        ).fetchall()
        return [r[0] for r in rows]

    # -----------------------------------------------------------------------
    # Relations
    # -----------------------------------------------------------------------

    def create_relation(
        self,
        vault_id: int,
        predicate: str,
        subject_entity_id: Optional[int] = None,
        object_entity_id: Optional[int] = None,
        object_text: Optional[str] = None,
        claim_id: Optional[int] = None,
        confidence: float = 0.0,
    ) -> WikiRelation:
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            """INSERT INTO wiki_relations
               (vault_id, subject_entity_id, predicate, object_entity_id, object_text, claim_id, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (vault_id, subject_entity_id, predicate, object_entity_id, object_text, claim_id, confidence, now),
        )
        self._db.commit()
        row = self._db.execute("SELECT * FROM wiki_relations WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _to_wiki_relation(row)

    def find_relation(
        self,
        vault_id: int,
        predicate: str,
        subject_entity_id: Optional[int],
        object_entity_id: Optional[int],
    ) -> Optional[WikiRelation]:
        """Return an existing relation matching vault + key triple, or None."""
        row = self._db.execute(
            """SELECT * FROM wiki_relations
               WHERE vault_id = ? AND predicate = ?
                 AND subject_entity_id IS ? AND object_entity_id IS ?
               LIMIT 1""",
            (vault_id, predicate, subject_entity_id, object_entity_id),
        ).fetchone()
        return _to_wiki_relation(row) if row else None

    def list_relations(self, vault_id: int, entity_id: Optional[int] = None) -> list[WikiRelation]:
        if entity_id is not None:
            rows = self._db.execute(
                "SELECT * FROM wiki_relations WHERE vault_id = ? AND (subject_entity_id = ? OR object_entity_id = ?)",
                (vault_id, entity_id, entity_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM wiki_relations WHERE vault_id = ?", (vault_id,)
            ).fetchall()
        return [_to_wiki_relation(r) for r in rows]

    # -----------------------------------------------------------------------
    # Compile Jobs
    # -----------------------------------------------------------------------

    def create_job(
        self,
        vault_id: int,
        trigger_type: str,
        trigger_id: Optional[str] = None,
        input_json: Optional[Any] = None,
    ) -> WikiCompileJob:
        now = datetime.utcnow().isoformat()
        if isinstance(input_json, dict):
            input_json = json.dumps(input_json)
        cur = self._db.execute(
            """INSERT INTO wiki_compile_jobs (vault_id, trigger_type, trigger_id, status, input_json, created_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (vault_id, trigger_type, trigger_id, input_json or "{}", now),
        )
        self._db.commit()
        row = self._db.execute("SELECT * FROM wiki_compile_jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _to_compile_job(row)

    def list_jobs(
        self,
        vault_id: int,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        trigger_type: Optional[str] = None,
        trigger_id: Optional[str] = None,
    ) -> list[WikiCompileJob]:
        # F-012: optional bound so callers that only need recent jobs don't pull
        # the vault's entire job history. trigger_type/trigger_id let status
        # endpoints filter in SQL instead of pulling and filtering in Python.
        sql = "SELECT * FROM wiki_compile_jobs WHERE vault_id = ?"
        params: list[Any] = [vault_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if trigger_type is not None:
            sql += " AND trigger_type = ?"
            params.append(trigger_type)
        if trigger_id is not None:
            sql += " AND trigger_id = ?"
            params.append(trigger_id)
        sql += " ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._db.execute(sql, params).fetchall()
        return [_to_compile_job(r) for r in rows]

    def get_job(self, job_id: int, vault_id: int) -> Optional[WikiCompileJob]:
        row = self._db.execute(
            "SELECT * FROM wiki_compile_jobs WHERE id = ? AND vault_id = ?",
            (job_id, vault_id),
        ).fetchone()
        return _to_compile_job(row) if row else None

    def claim_next_pending_job(self) -> Optional[WikiCompileJob]:
        """Atomically claim the oldest pending job. Returns the claimed job or None.

        Uses a single ``UPDATE ... RETURNING`` statement so the claim is atomic
        without a backend-specific transaction mode such as SQLite's
        ``BEGIN IMMEDIATE``. The query is portable across SQLite (>= 3.35) and
        PostgreSQL: the inner ``SELECT`` pins the oldest pending row and the
        outer ``UPDATE`` flips it to ``running`` in one atomic step, so two
        concurrent callers can never claim the same job.
        """
        now = datetime.utcnow().isoformat()
        row = self._db.execute(
            """
            UPDATE wiki_compile_jobs
            SET status = 'running', started_at = ?
            WHERE id = (
                SELECT id FROM wiki_compile_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
            )
            RETURNING *
            """,
            (now,),
        ).fetchone()
        if row:
            self._db.commit()
            return _to_compile_job(row)
        # No pending job matched. Python's sqlite3 opens an implicit transaction
        # for any DML (even a 0-row UPDATE), so roll it back to leave the
        # connection clean rather than holding an open read/write transaction.
        self._db.rollback()
        return None

    def complete_job(self, job_id: int, result_json: Any) -> None:
        """Mark job completed. No-op if the job was already cancelled."""
        row = self._db.execute(
            "SELECT status FROM wiki_compile_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row and dict(row)["status"] == "cancelled":
            return
        now = datetime.utcnow().isoformat()
        if isinstance(result_json, dict):
            result_json = json.dumps(result_json)
        self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'completed', completed_at = ?, result_json = ? WHERE id = ?",
            (now, result_json or "{}", job_id),
        )
        self._db.commit()

    def fail_job(self, job_id: int, error: str) -> int:
        """Mark job failed, increment retry_count. Returns new retry_count.

        No-op if the job is already cancelled.
        """
        now = datetime.utcnow().isoformat()
        self._db.execute(
            """UPDATE wiki_compile_jobs
               SET status = 'failed', completed_at = ?, error = ?, retry_count = retry_count + 1
               WHERE id = ? AND status != 'cancelled'""",
            (now, error[:8000], job_id),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT retry_count FROM wiki_compile_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row)["retry_count"] if row else 0

    def reset_job_to_pending(self, job_id: int) -> None:
        """Reset a failed job back to pending for auto-retry by the processor."""
        self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'pending', started_at = NULL, completed_at = NULL WHERE id = ?",
            (job_id,),
        )
        self._db.commit()

    def cancel_job(self, job_id: int, vault_id: int) -> bool:
        """Cancel a pending or running job. Returns True if cancelled."""
        row = self._db.execute(
            "SELECT status FROM wiki_compile_jobs WHERE id = ? AND vault_id = ?",
            (job_id, vault_id),
        ).fetchone()
        if not row or dict(row)["status"] not in ("pending", "running"):
            return False
        self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'cancelled', completed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), job_id),
        )
        self._db.commit()
        return True

    def retry_job(self, job_id: int, vault_id: int) -> Optional[WikiCompileJob]:
        """Reset a failed job to pending. Returns the updated job or None."""
        row = self._db.execute(
            "SELECT status FROM wiki_compile_jobs WHERE id = ? AND vault_id = ?",
            (job_id, vault_id),
        ).fetchone()
        if not row or dict(row)["status"] != "failed":
            return None
        self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'pending', error = NULL, started_at = NULL, completed_at = NULL WHERE id = ?",
            (job_id,),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT * FROM wiki_compile_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _to_compile_job(row) if row else None

    def reset_running_jobs(self) -> int:
        """Reset any jobs stuck in 'running' to 'pending' on processor startup.

        Returns the number of jobs reset (orphans from a previous crash).
        """
        cur = self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'pending', started_at = NULL WHERE status = 'running'"
        )
        self._db.commit()
        return cur.rowcount

    def mark_claims_stale_by_file(self, file_id: int, vault_id: int) -> dict:
        """Mark wiki claims stale when their source document is deleted/reindexed.

        - Claims whose ONLY source was this file are set status='stale'.
        - Claims with other sources get a 'weak_provenance' lint finding only.
        Returns counts of stale/weak findings created.
        """
        stale_count = 0
        weak_count = 0
        now = datetime.utcnow().isoformat()

        source_rows = self._db.execute(
            "SELECT DISTINCT claim_id FROM wiki_claim_sources WHERE file_id = ? AND claim_id IS NOT NULL",
            (file_id,),
        ).fetchall()

        for sr in source_rows:
            claim_id = dict(sr)["claim_id"]
            claim_row = self._db.execute(
                "SELECT vault_id, status, claim_text FROM wiki_claims WHERE id = ? AND vault_id = ?",
                (claim_id, vault_id),
            ).fetchone()
            if not claim_row:
                continue
            other_sources = self._db.execute(
                "SELECT COUNT(*) FROM wiki_claim_sources WHERE claim_id = ? AND (file_id != ? OR file_id IS NULL)",
                (claim_id, file_id),
            ).fetchone()[0]
            if other_sources == 0:
                self._db.execute(
                    "UPDATE wiki_claims SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, claim_id),
                )
                self._db.execute(
                    """INSERT INTO wiki_lint_findings
                       (vault_id, finding_type, severity, title, details,
                        related_page_ids_json, related_claim_ids_json, status, created_at, updated_at)
                       VALUES (?, 'stale', 'medium', ?, ?, '[]', ?, 'open', ?, ?)""",
                    (
                        vault_id,
                        f"Claim stale: source document deleted (file_id={file_id})",
                        dict(claim_row)["claim_text"][:200],
                        json.dumps([claim_id]),
                        now, now,
                    ),
                )
                stale_count += 1
            else:
                self._db.execute(
                    """INSERT INTO wiki_lint_findings
                       (vault_id, finding_type, severity, title, details,
                        related_page_ids_json, related_claim_ids_json, status, created_at, updated_at)
                       VALUES (?, 'weak_provenance', 'low', ?, ?, '[]', ?, 'open', ?, ?)""",
                    (
                        vault_id,
                        f"Weak provenance: source document deleted (file_id={file_id})",
                        dict(claim_row)["claim_text"][:200],
                        json.dumps([claim_id]),
                        now, now,
                    ),
                )
                weak_count += 1

        self._db.commit()
        return {"stale": stale_count, "weak_provenance": weak_count}

    def mark_claims_stale_by_memory(self, memory_id: int, vault_id: int) -> dict:
        """Mark wiki claims stale when their source memory is edited or deleted.

        Same policy as mark_claims_stale_by_file: sole-source → stale,
        multi-source → weak_provenance lint finding.
        """
        stale_count = 0
        weak_count = 0
        now = datetime.utcnow().isoformat()

        source_rows = self._db.execute(
            "SELECT DISTINCT claim_id FROM wiki_claim_sources WHERE memory_id = ? AND claim_id IS NOT NULL",
            (memory_id,),
        ).fetchall()

        for sr in source_rows:
            claim_id = dict(sr)["claim_id"]
            claim_row = self._db.execute(
                "SELECT vault_id, status, claim_text FROM wiki_claims WHERE id = ? AND vault_id = ?",
                (claim_id, vault_id),
            ).fetchone()
            if not claim_row:
                continue
            other_sources = self._db.execute(
                "SELECT COUNT(*) FROM wiki_claim_sources WHERE claim_id = ? AND (memory_id != ? OR memory_id IS NULL)",
                (claim_id, memory_id),
            ).fetchone()[0]
            if other_sources == 0:
                self._db.execute(
                    "UPDATE wiki_claims SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, claim_id),
                )
                self._db.execute(
                    """INSERT INTO wiki_lint_findings
                       (vault_id, finding_type, severity, title, details,
                        related_page_ids_json, related_claim_ids_json, status, created_at, updated_at)
                       VALUES (?, 'stale', 'medium', ?, ?, '[]', ?, 'open', ?, ?)""",
                    (
                        vault_id,
                        f"Claim stale: source memory edited/deleted (memory_id={memory_id})",
                        dict(claim_row)["claim_text"][:200],
                        json.dumps([claim_id]),
                        now, now,
                    ),
                )
                stale_count += 1
            else:
                self._db.execute(
                    """INSERT INTO wiki_lint_findings
                       (vault_id, finding_type, severity, title, details,
                        related_page_ids_json, related_claim_ids_json, status, created_at, updated_at)
                       VALUES (?, 'weak_provenance', 'low', ?, ?, '[]', ?, 'open', ?, ?)""",
                    (
                        vault_id,
                        f"Weak provenance: source memory edited/deleted (memory_id={memory_id})",
                        dict(claim_row)["claim_text"][:200],
                        json.dumps([claim_id]),
                        now, now,
                    ),
                )
                weak_count += 1

        self._db.commit()
        return {"stale": stale_count, "weak_provenance": weak_count}

    def update_lint_finding(self, finding_id: int, vault_id: int, status: str) -> Optional[WikiLintFinding]:
        """Resolve or dismiss a lint finding. Returns updated finding or None."""
        if status not in ("resolved", "dismissed", "acknowledged"):
            raise ValueError(f"Invalid lint finding status: {status!r}")
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            "UPDATE wiki_lint_findings SET status = ?, updated_at = ? WHERE id = ? AND vault_id = ?",
            (status, now, finding_id, vault_id),
        )
        if cur.rowcount == 0:
            return None
        self._db.commit()
        row = self._db.execute(
            "SELECT * FROM wiki_lint_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        return _to_lint_finding(row) if row else None

    # -----------------------------------------------------------------------
    # Lint Findings
    # -----------------------------------------------------------------------

    def create_lint_finding(
        self,
        vault_id: int,
        finding_type: str,
        title: str,
        severity: str = "medium",
        details: str = "",
        related_page_ids: Optional[list] = None,
        related_claim_ids: Optional[list] = None,
    ) -> WikiLintFinding:
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            """INSERT INTO wiki_lint_findings
               (vault_id, finding_type, severity, title, details,
                related_page_ids_json, related_claim_ids_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (vault_id, finding_type, severity, title, details,
             json.dumps(related_page_ids or []), json.dumps(related_claim_ids or []), now, now),
        )
        self._db.commit()
        row = self._db.execute("SELECT * FROM wiki_lint_findings WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _to_lint_finding(row)

    def list_lint_findings(
        self,
        vault_id: int,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        page_id: Optional[int] = None,
    ) -> list[WikiLintFinding]:
        sql = "SELECT * FROM wiki_lint_findings WHERE vault_id = ?"
        params: list[Any] = [vault_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if page_id is not None:
            # Use json_each to check membership — avoids LIKE false-positives where
            # page_id=1 would also match arrays containing 10, 11, 21, etc.
            sql += " AND EXISTS (SELECT 1 FROM json_each(related_page_ids_json) WHERE value = ?)"
            params.append(page_id)
        sql += " ORDER BY severity DESC, created_at DESC"
        rows = self._db.execute(sql, params).fetchall()
        return [_to_lint_finding(r) for r in rows]

    def clear_open_findings(self, vault_id: int) -> None:
        self._db.execute(
            "DELETE FROM wiki_lint_findings WHERE vault_id = ? AND status = 'open'", (vault_id,)
        )
        self._db.commit()

    # -----------------------------------------------------------------------
    # Global Search
    # -----------------------------------------------------------------------

    def search(
        self,
        vault_id: int,
        query: str,
        limit: int = 20,
        page_type: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        page_ids = self._fts_page_ids(vault_id, query)
        claim_ids = self._fts_claim_ids(vault_id, query)
        entity_ids = self._fts_entity_ids(vault_id, query)

        pages = []
        if page_ids:
            ph = ",".join("?" * min(len(page_ids), limit))
            sql = f"SELECT * FROM wiki_pages WHERE id IN ({ph}) AND vault_id = ?"
            params: list[Any] = [*page_ids[:limit], vault_id]
            if page_type:
                sql += " AND page_type = ?"
                params.append(page_type)
            if status:
                sql += " AND status = ?"
                params.append(status)
            allowed_sorts = {"updated_at", "created_at", "title", "confidence"}
            order_col = sort_by if sort_by in allowed_sorts else "updated_at"
            order_dir = "ASC" if order_col == "title" else "DESC"
            sql += f" ORDER BY {order_col} {order_dir} LIMIT ?"
            params.append(limit)
            rows = self._db.execute(sql, params).fetchall()
            pages = [_to_wiki_page(r) for r in rows]

        claims = []
        if claim_ids:
            ph = ",".join("?" * min(len(claim_ids), limit))
            rows = self._db.execute(
                f"SELECT * FROM wiki_claims WHERE id IN ({ph}) AND vault_id = ? LIMIT ?",
                [*claim_ids[:limit], vault_id, limit],
            ).fetchall()
            claims = [_to_wiki_claim(r) for r in rows]

        entities = []
        if entity_ids:
            ph = ",".join("?" * min(len(entity_ids), limit))
            rows = self._db.execute(
                f"SELECT * FROM wiki_entities WHERE id IN ({ph}) AND vault_id = ? LIMIT ?",
                [*entity_ids[:limit], vault_id, limit],
            ).fetchall()
            entities = [_to_wiki_entity(r) for r in rows]

        return {"pages": pages, "claims": claims, "entities": entities, "query": query}

    # -----------------------------------------------------------------------
    # Version History (DD-C003)
    # -----------------------------------------------------------------------

    def save_version(
        self,
        page_id: int,
        vault_id: int,
        edited_by: Optional[int] = None,
        commit: bool = True,
    ) -> Optional[WikiPageVersion]:
        """Snapshot current page state into wiki_page_versions before an update."""
        row = self._db.execute(
            "SELECT * FROM wiki_pages WHERE id = ? AND vault_id = ?",
            (page_id, vault_id),
        ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            """INSERT INTO wiki_page_versions
               (page_id, vault_id, title, markdown, summary, status, confidence, edited_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (page_id, vault_id, d["title"], d["markdown"], d.get("summary") or "",
             d["status"], d.get("confidence") or 0.0, edited_by, now),
        )
        if commit:
            self._db.commit()
        vrow = self._db.execute(
            "SELECT * FROM wiki_page_versions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _to_page_version(vrow)

    def list_versions(self, page_id: int, limit: int = 20) -> list[WikiPageVersion]:
        """List version history for a page, most recent first."""
        rows = self._db.execute(
            "SELECT * FROM wiki_page_versions WHERE page_id = ? ORDER BY created_at DESC LIMIT ?",
            (page_id, limit),
        ).fetchall()
        return [_to_page_version(r) for r in rows]

    # -----------------------------------------------------------------------
    # File Attachments (DD-C019)
    # -----------------------------------------------------------------------

    def attach_file(self, page_id: int, file_id: int, vault_id: int) -> Optional[WikiPageFile]:
        """Attach a file to a wiki page.

        Uses a plain INSERT so a duplicate (page_id, file_id) raises
        ``sqlite3.IntegrityError`` against the UNIQUE constraint. The route layer
        translates that into a 409 — using ``INSERT OR IGNORE`` here would
        silently swallow the conflict and make that 409 handler dead code.
        """
        now = datetime.utcnow().isoformat()
        try:
            self._db.execute(
                """INSERT INTO wiki_page_files (page_id, file_id, vault_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (page_id, file_id, vault_id, now),
            )
        except sqlite3.IntegrityError:
            self._db.rollback()
            raise
        self._db.commit()
        row = self._db.execute(
            "SELECT * FROM wiki_page_files WHERE page_id = ? AND file_id = ?",
            (page_id, file_id),
        ).fetchone()
        return _to_page_file(row) if row else None

    def detach_file(self, page_id: int, file_id: int, vault_id: int) -> bool:
        """Remove a file attachment from a wiki page."""
        cur = self._db.execute(
            "DELETE FROM wiki_page_files WHERE page_id = ? AND file_id = ? AND vault_id = ?",
            (page_id, file_id, vault_id),
        )
        self._db.commit()
        return cur.rowcount > 0

    def list_page_files(self, page_id: int) -> list[WikiPageFile]:
        """List files attached to a wiki page."""
        rows = self._db.execute(
            "SELECT * FROM wiki_page_files WHERE page_id = ? ORDER BY created_at",
            (page_id,),
        ).fetchall()
        return [_to_page_file(r) for r in rows]

    # -----------------------------------------------------------------------
    # Wiki Links (DD-C030)
    # -----------------------------------------------------------------------

    def sync_page_links(
        self, page_id: int, vault_id: int, markdown: str, commit: bool = True
    ) -> list[WikiPageLink]:
        """Parse [[slug]] and [[slug|display]] from markdown, resolve to page IDs,
        replace old links.

        ``commit=False`` lets a caller (e.g. ``update_page``) fold the link sync
        into its own transaction so the whole save is atomic.
        """
        # Extract all [[...]] wiki-link targets. F-006: support the
        # ``[[slug|display text]]`` form — the part before the pipe is the link
        # target, the part after is the human-readable label.
        raw_targets = re.findall(r"\[\[([^\]]+)\]\]", markdown)
        # (normalized_slug, link_text) pairs in document order.
        parsed: list[tuple[str, str]] = []
        for raw in raw_targets:
            target_part, _, display_part = raw.partition("|")
            slug = normalize_slug(target_part.strip())
            if not slug:
                continue
            link_text = (display_part.strip() or target_part.strip())
            parsed.append((slug, link_text))

        # F-009: resolve every distinct slug in a single query instead of one
        # SELECT per link.
        resolved: list[tuple[int, str]] = []
        unique_slugs = list({slug for slug, _ in parsed})
        if unique_slugs:
            placeholders = ",".join("?" * len(unique_slugs))
            rows_by_slug = {
                r["slug"]: r["id"]
                for r in self._db.execute(
                    f"SELECT id, slug FROM wiki_pages WHERE vault_id = ? AND slug IN ({placeholders})",
                    [vault_id, *unique_slugs],
                ).fetchall()
            }
            for slug, link_text in parsed:
                target_id = rows_by_slug.get(slug)
                if target_id is not None:
                    resolved.append((target_id, link_text))

        # Replace old links for this source page
        self._db.execute(
            "DELETE FROM wiki_page_links WHERE source_page_id = ? AND vault_id = ?",
            (page_id, vault_id),
        )
        now = datetime.utcnow().isoformat()
        for target_id, link_text in resolved:
            if target_id == page_id:
                continue  # skip self-links
            self._db.execute(
                """INSERT OR IGNORE INTO wiki_page_links
                   (source_page_id, target_page_id, vault_id, link_text, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (page_id, target_id, vault_id, link_text, now),
            )
        if commit:
            self._db.commit()

        rows = self._db.execute(
            "SELECT * FROM wiki_page_links WHERE source_page_id = ? AND vault_id = ?",
            (page_id, vault_id),
        ).fetchall()
        return [_to_page_link(r) for r in rows]

    def list_backlinks(self, page_id: int, vault_id: int) -> list[WikiPageLink]:
        """List pages that link TO this page (scoped to the page's vault)."""
        rows = self._db.execute(
            "SELECT * FROM wiki_page_links WHERE target_page_id = ? AND vault_id = ? "
            "ORDER BY created_at DESC",
            (page_id, vault_id),
        ).fetchall()
        return [_to_page_link(r) for r in rows]

    # -----------------------------------------------------------------------
    # Activity Log (DD-C026)
    # -----------------------------------------------------------------------

    def log_activity(
        self,
        vault_id: int,
        action: str,
        target_type: str,
        target_id: int,
        user_id: Optional[int] = None,
        details: Optional[dict] = None,
        commit: bool = True,
    ) -> WikiActivityEntry:
        """Insert an entry into the wiki activity log."""
        now = datetime.utcnow().isoformat()
        details_json = json.dumps(details) if details else "{}"
        cur = self._db.execute(
            """INSERT INTO wiki_activity_log
               (vault_id, action, target_type, target_id, user_id, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (vault_id, action, target_type, target_id, user_id, details_json, now),
        )
        if commit:
            self._db.commit()
        row = self._db.execute(
            "SELECT * FROM wiki_activity_log WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _to_activity_entry(row)

    def list_activity(self, vault_id: int, limit: int = 50) -> list[WikiActivityEntry]:
        """List recent activity for a vault."""
        rows = self._db.execute(
            "SELECT * FROM wiki_activity_log WHERE vault_id = ? ORDER BY created_at DESC LIMIT ?",
            (vault_id, limit),
        ).fetchall()
        return [_to_activity_entry(r) for r in rows]

    # -----------------------------------------------------------------------
    # Improved Claim Dedup (DD-C011)
    # -----------------------------------------------------------------------

    def find_claim_by_normalized_text(self, vault_id: int, claim_text: str) -> Optional[WikiClaim]:
        """Find a claim by normalized text using the indexed normalized_text column."""
        normalized = normalize_claim_text(claim_text)
        if not normalized:
            return None
        row = self._db.execute(
            "SELECT * FROM wiki_claims WHERE vault_id = ? AND normalized_text = ? LIMIT 1",
            (vault_id, normalized),
        ).fetchone()
        if row is None:
            return None
        claim = _to_wiki_claim(row)
        claim.sources = self._load_sources(claim.id)
        return claim

    # -----------------------------------------------------------------------
    # Bulk Operations (DD-C025)
    # -----------------------------------------------------------------------

    def bulk_update_pages(self, page_ids: list[int], vault_id: int, **kwargs: Any) -> list[WikiPage]:
        """Update multiple pages atomically. Either all succeed or none are committed."""
        results: list[WikiPage] = []
        try:
            for pid in page_ids:
                page = self.update_page(pid, vault_id, commit=False, **kwargs)
                if page:
                    results.append(page)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return results

    def bulk_delete_pages(self, page_ids: list[int], vault_id: int) -> int:
        """Delete multiple pages atomically. Either all succeed or none are committed."""
        count = 0
        try:
            for pid in page_ids:
                if self.delete_page(pid, vault_id, commit=False):
                    count += 1
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return count
