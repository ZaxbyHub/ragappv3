"""
WikiStore: CRUD operations for all wiki / Knowledge Compiler tables.

All operations are vault-scoped. Slug normalization is enforced on create/update.
FTS search is backed by wiki_pages_fts, wiki_claims_fts, wiki_entities_fts.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional

from app.services.fts_query import FTS_CANDIDATE_CAP, build_fts_match_query

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
    # AC40 (#515): display name resolved via a LEFT JOIN onto ``files`` so the
    # UI can label attachments without a second round-trip. None when the
    # joined files row is missing (or the row was built without the join).
    filename: Optional[str] = None


@dataclass
class WikiPageLink:
    id: int
    source_page_id: int
    target_page_id: int
    vault_id: int
    link_text: Optional[str]
    created_at: str
    # AC40 (#515): source-page display fields resolved via a LEFT JOIN onto
    # ``wiki_pages`` so backlink rows carry the linking page's title/slug.
    # None when the source page was deleted or the row was built without the
    # join.
    source_title: Optional[str] = None
    source_slug: Optional[str] = None


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

class PageList(list):
    """``list[WikiPage]`` carrying pagination metadata (AC34 / issue #515).

    Subclasses ``list`` so every existing consumer (iteration, ``len``,
    indexing, equality) is unchanged; the API route reads ``.total`` /
    ``.page`` / ``.per_page`` to expose pagination in the response envelope
    so the UI can offer a Load-more control while a vault grows past one
    page. ``total`` counts rows matching the SAME filtered WHERE clause (no
    LIMIT/OFFSET).
    """

    def __init__(
        self,
        pages: Optional[list] = None,
        *,
        total: int = 0,
        page: int = 1,
        per_page: int = 50,
    ) -> None:
        super().__init__(pages or [])
        self.total = total
        self.page = page
        self.per_page = per_page


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
        filename=d.get("filename"),
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
        source_title=d.get("source_title"),
        source_slug=d.get("source_slug"),
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

    # SEARCH-003 (#515): chunk any SQL ``IN (...)`` list fed to a store search
    # so it stays under SQLite's default 999 host-variable limit.
    _SQL_IN_CHUNK = 900

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Explicit ``BEGIN IMMEDIATE ... COMMIT/ROLLBACK`` transaction.

        WIKI-010 (#515): multi-statement persistence sequences (claim +
        sources) must commit atomically instead of relying on whatever
        write happens to come next (lint findings, a later job) to flush
        the pending inserts. BEGIN IMMEDIATE takes the write lock up
        front so the block serialises against concurrent writers.

        Precondition: the connection must not already be inside a
        transaction (every store write path commits before returning);
        SQLite would otherwise reject the nested BEGIN. On any exception
        the block rolls back and re-raises, so partial writes never
        become visible to other connections.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.rollback()
            raise
        self._db.commit()

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
    ) -> PageList:
        """List pages for a vault, most recently updated first.

        AC34 (#515): returns a ``PageList`` — a plain list of pages plus
        ``.total`` counting every row matching the SAME WHERE clause, so the
        route can expose ``total`` for Load-more pagination.
        """
        offset = (page - 1) * per_page
        where = "vault_id = ?"
        params: list[Any] = [vault_id]
        if search:
            ids = self._fts_page_ids(vault_id, search)
            if not ids:
                return PageList([], total=0, page=page, per_page=per_page)
            placeholders = ",".join("?" * len(ids))
            where = f"id IN ({placeholders}) AND vault_id = ?"
            params = [*ids, vault_id]
        if page_type:
            where += " AND page_type = ?"
            params.append(page_type)
        if status:
            where += " AND status = ?"
            params.append(status)
        total = self._db.execute(
            f"SELECT COUNT(*) FROM wiki_pages WHERE {where}",  # nosec B608 — where is built from fixed fragments with bound params
            params,
        ).fetchone()[0]
        rows = self._db.execute(
            f"SELECT * FROM wiki_pages WHERE {where} "  # nosec B608 — see above
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, per_page, offset],
        ).fetchall()
        return PageList(
            [_to_wiki_page(r) for r in rows], total=total, page=page, per_page=per_page
        )

    def update_page(
        self,
        page_id: int,
        vault_id: int,
        expected_version: Optional[int] = None,
        edited_by: Optional[int] = None,
        commit: bool = True,
        **kwargs: Any,
    ) -> Optional[WikiPage]:
        """Update page columns; only kwargs present in ``allowed`` are written.

        AC17 (#515) null semantics: an explicitly-passed ``parent_id=None``
        CLEARS the parent (``SET parent_id = NULL``); an ABSENT ``parent_id``
        kwarg leaves it untouched. The route layer decides which is which via
        ``request.model_fields_set`` — the store cannot distinguish them
        itself because a plain ``None`` value in ``**kwargs`` is a deliberate
        "clear" instruction, not a "skip". Same for other nullable columns
        (``summary``); non-nullable columns must never be passed as None.
        """
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
            # SPEC section 12.6: a page-body change invalidates every Draft Room
            # evidence row carrying this wiki_page_id. Best effort — the hook
            # cannot raise, and the Draft Room re-resolves evidence again before
            # Assemble/Ready regardless.
            from app.services.draft_evidence_freshness import on_wiki_page_changed

            on_wiki_page_changed(
                self._db, page_id=page_id, new_markdown=updates["markdown"]
            )
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
            # SPEC section 12.6 (best effort, cannot raise).
            from app.services.draft_evidence_freshness import on_wiki_page_changed

            on_wiki_page_changed(self._db, page_id=page_id, new_markdown=None)
        if commit:
            self._db.commit()
        return deleted

    def _fts_page_ids(self, vault_id: int, query: str) -> list[int]:
        """FTS candidate page ids for ``query`` (bm25-ranked, bounded).

        SEARCH-004 (#515): the raw query is tokenized via the shared
        ``fts_query.build_fts_match_query`` helper, so an ordinary hyphenated
        term like ``Model-X`` reaches FTS5 as ``model* x*`` instead of raw
        column-filter syntax that raises ``OperationalError``. The emitted
        tokens always match ``\\w+``, so the MATCH input is structurally safe.

        SEARCH-003 (#515): ids are rank-ordered and capped ONLY to bound the
        SQL ``IN`` list in callers; visible filtering, ordering, and the
        result cap are decided by the caller's SQL over the returned set.
        """
        fts_query = build_fts_match_query(query)
        if not fts_query:
            return []
        rows = self._db.execute(
            "SELECT rowid FROM wiki_pages_fts WHERE wiki_pages_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, FTS_CANDIDATE_CAP),
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
        """FTS candidate entity ids for ``query`` — see ``_fts_page_ids``."""
        fts_query = build_fts_match_query(query)
        if not fts_query:
            return []
        rows = self._db.execute(
            "SELECT rowid FROM wiki_entities_fts WHERE wiki_entities_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, FTS_CANDIDATE_CAP),
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
        commit: bool = True,
    ) -> WikiClaim:
        """Create a claim row (plus optional inline sources).

        ``commit=False`` leaves the write inside the caller's open
        ``store.transaction()`` block so the claim and its subsequent
        ``attach_source`` inserts commit atomically (WIKI-010 / issue #515 —
        the curator's accepted-claim persistence must not expose a committed
        claim whose source row is still pending).
        """
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
        if commit:
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

    def reactivate_claim(self, claim_id: int) -> bool:
        """Transition a ``superseded`` claim back to ``active``.

        WIKI-006 (#515): when a recompile re-derives a claim whose text is
        unchanged from a still-supported source, the claim is proof again
        and must not stay buried as superseded. The UPDATE's
        ``status = 'superseded'`` predicate makes the check-and-flip atomic,
        so a claim that concurrently reached any other state can never be
        blanket-reactivated. Returns True when the transition committed.
        """
        cur = self._db.execute(
            "UPDATE wiki_claims SET status = 'active', updated_at = ? "
            "WHERE id = ? AND status = 'superseded'",
            (datetime.utcnow().isoformat(), claim_id),
        )
        if cur.rowcount == 0:
            # Nothing transitioned (already active / resolved elsewhere, or
            # the row is gone). Roll back the implicit transaction Python's
            # sqlite3 opened for the 0-row UPDATE so the connection stays clean.
            self._db.rollback()
            return False
        self._db.commit()
        return True

    def get_claim_sources(self, claim_id: int) -> list[WikiClaimSource]:
        """List a claim's source rows by claim id.

        WIKI-008 (#515): claim-reuse paths must reload provenance by the
        CLAIM ID they already hold. Looking the claim up again by exact
        input text misses normalized-equivalent matches (punctuation /
        whitespace variants) and yields an empty source snapshot, which
        then re-attaches duplicate source rows.
        """
        return self._load_sources(claim_id)

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
        if "claim_text" in updates:
            # SPEC section 12.6: a claim change invalidates evidence rows
            # carrying this wiki_claim_id. Best effort, cannot raise.
            from app.services.draft_evidence_freshness import on_wiki_claim_changed

            on_wiki_claim_changed(
                self._db, claim_id=claim_id, new_claim_text=updates["claim_text"]
            )
        self._db.commit()
        return self.get_claim(claim_id)

    def delete_claim(self, claim_id: int, vault_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM wiki_claims WHERE id = ? AND vault_id = ?", (claim_id, vault_id)
        )
        if cur.rowcount > 0:
            # SPEC section 12.6 (best effort, cannot raise).
            from app.services.draft_evidence_freshness import on_wiki_claim_changed

            on_wiki_claim_changed(self._db, claim_id=claim_id, new_claim_text=None)
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
        """FTS candidate claim ids for ``query`` — see ``_fts_page_ids``."""
        fts_query = build_fts_match_query(query)
        if not fts_query:
            return []
        rows = self._db.execute(
            "SELECT rowid FROM wiki_claims_fts WHERE wiki_claims_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, FTS_CANDIDATE_CAP),
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
        try:
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
        except Exception:
            self._db.rollback()
            raise

    def complete_job(self, job_id: int, result_json: Any) -> bool:
        """Mark job completed. Returns True if the transition committed.

        No-op (returns False) if the job was already cancelled: the UPDATE's
        ``status != 'cancelled'`` predicate is evaluated atomically with the
        write, so a cancellation landing between this method's call and its
        write can never be overwritten (WIKI-004 / issue #515 — the previous
        read-then-write guard raced in exactly that window).
        """
        now = datetime.utcnow().isoformat()
        if isinstance(result_json, dict):
            result_json = json.dumps(result_json)
        cur = self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'completed', completed_at = ?, result_json = ? "
            "WHERE id = ? AND status != 'cancelled'",
            (now, result_json or "{}", job_id),
        )
        if cur.rowcount == 0:
            # No transition committed (cancelled, or the row is gone). Roll
            # back the implicit transaction Python's sqlite3 opened for the
            # 0-row UPDATE so the connection is left clean.
            self._db.rollback()
            return False
        self._db.commit()
        return True

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
        """Reset a failed job back to pending for auto-retry by the processor.

        Guarded by ``status = 'failed'`` so the auto-retry path cannot clobber a
        job that has since been retried-then-cancelled by the user during the
        processor's backoff window (issue #276 A6-1). The processor only calls
        this immediately after ``fail_job`` set status='failed', so the guard is
        a no-op on the intended path; every sibling state-transition method
        (complete_job, fail_job, cancel_job, retry_job) guards the transition
        the same atomic way — a conditional UPDATE predicate, not a
        read-then-write check that can race (issue #515).
        """
        self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'pending', started_at = NULL, completed_at = NULL "
            "WHERE id = ? AND status = 'failed'",
            (job_id,),
        )
        self._db.commit()

    def cancel_job(self, job_id: int, vault_id: int, result_json: Optional[Any] = None) -> bool:
        """Cancel a pending or running job. Returns True if cancelled.

        The ``status IN ('pending','running')`` predicate makes the
        check-and-flip atomic, so a job that already reached a terminal state
        (completed / failed / cancelled) can never be resurrected or
        double-terminalised by a late cancel (issue #515).

        ``result_json`` (WIKI-001 / issue #515) lets the worker record WHY a
        job was cancelled — e.g. ``{"cancelled": "wiki_compile_disabled"}``
        when the feature flags went off between enqueue and dispatch — so the
        cancellation is auditable from the job row itself.
        """
        if isinstance(result_json, dict):
            result_json = json.dumps(result_json)
        set_result = ", result_json = ?" if result_json else ""
        params: list[Any] = [datetime.utcnow().isoformat()]
        if result_json:
            params.append(result_json)
        cur = self._db.execute(
            f"UPDATE wiki_compile_jobs SET status = 'cancelled', completed_at = ?{set_result} "
            f"WHERE id = ? AND vault_id = ? AND status IN ('pending', 'running')",  # nosec B608 — fragments are fixed literals
            [*params, job_id, vault_id],
        )
        if cur.rowcount == 0:
            self._db.rollback()
            return False
        self._db.commit()
        return True

    def retry_job(self, job_id: int, vault_id: int) -> Optional[WikiCompileJob]:
        """Reset a failed job to pending. Returns the updated job or None.

        The ``status = 'failed'`` predicate makes the check-and-flip atomic:
        a job that was concurrently cancelled or completed cannot be reset.
        """
        cur = self._db.execute(
            "UPDATE wiki_compile_jobs SET status = 'pending', error = NULL, started_at = NULL, completed_at = NULL "
            "WHERE id = ? AND vault_id = ? AND status = 'failed'",
            (job_id, vault_id),
        )
        if cur.rowcount == 0:
            self._db.rollback()
            return None
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

        # NOTE: do NOT commit here. The caller controls the transaction
        # boundary so that the stale marking can be made atomic with the
        # subsequent DELETE of the source row (file/memory). If this method
        # committed independently and the caller's DELETE later failed and
        # rolled back, the claim would be marked stale while the source row
        # still exists, leaving orphan lint findings (DD-C009 / #108).
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

        # NOTE: do NOT commit here. See mark_claims_stale_by_file for the
        # rationale (DD-C009 / #108). The caller controls the transaction
        # boundary so the stale marking is atomic with the subsequent
        # DELETE of the source memory.
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
        """Insert one OPEN lint finding and commit.

        AC35 (#515): callers that re-detect the same issue across runs should
        prefer ``upsert_lint_findings`` (fingerprint-aware, respects
        dismissals); this direct insert is for one-off findings (e.g. claim
        invalidation writing 'stale'/'weak_provenance' rows).
        """
        return self._insert_lint_finding_row(
            vault_id=vault_id,
            finding_type=finding_type,
            title=title,
            severity=severity,
            details=details,
            related_page_ids=related_page_ids,
            related_claim_ids=related_claim_ids,
            commit=True,
        )

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
    # Lint finding identity + upsert (AC35 / issue #515)
    # -----------------------------------------------------------------------

    @staticmethod
    def lint_fingerprint(
        finding_type: str, title: str, related_page_ids: Optional[list] = None
    ) -> tuple:
        """Stable identity for a lint finding, derived from existing columns.

        AC35 (#515): ``finding_type`` (the rule) + the sorted related-page-id
        tuple + the normalized title (the message) identify the SAME logical
        finding across lint runs, with no schema change. Severity/details may
        drift without changing identity; whitespace/case differences in the
        title do not either.
        """
        pages = tuple(sorted({int(p) for p in (related_page_ids or []) if p}))
        normalized_title = re.sub(r"\s+", " ", (title or "").strip().lower())
        return (finding_type, pages, normalized_title)

    @staticmethod
    def _row_lint_fingerprint(row: sqlite3.Row) -> tuple:
        d = _row_to_dict(row)
        try:
            page_ids = json.loads(d.get("related_page_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            page_ids = []
        return WikiStore.lint_fingerprint(
            d["finding_type"], d.get("title") or "", page_ids
        )

    def _insert_lint_finding_row(
        self,
        vault_id: int,
        finding_type: str,
        title: str,
        severity: str = "medium",
        details: str = "",
        related_page_ids: Optional[list] = None,
        related_claim_ids: Optional[list] = None,
        commit: bool = True,
    ) -> WikiLintFinding:
        """INSERT one open lint finding (shared by create/upsert paths)."""
        now = datetime.utcnow().isoformat()
        cur = self._db.execute(
            """INSERT INTO wiki_lint_findings
               (vault_id, finding_type, severity, title, details,
                related_page_ids_json, related_claim_ids_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (vault_id, finding_type, severity, title, details,
             json.dumps(related_page_ids or []), json.dumps(related_claim_ids or []), now, now),
        )
        if commit:
            self._db.commit()
        row = self._db.execute(
            "SELECT * FROM wiki_lint_findings WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _to_lint_finding(row)

    def upsert_lint_findings(self, vault_id: int, specs: list) -> list:
        """Reconcile this run's DETECTED findings against existing rows.

        AC35 (#515): a dismissed/resolved finding must not resurrect as a new
        open row on the next lint run. Instead of clear-open-then-recreate:

        - a detected fingerprint that matches an existing NON-open row
          (dismissed / resolved / acknowledged) is SUPPRESSED — no new row;
        - a detected fingerprint with an existing open row keeps that row
          (no duplicate insert);
        - a detected fingerprint with no existing row inserts a fresh open
          finding;
        - an open row whose fingerprint was NOT detected this run transitions
          to 'resolved' (the underlying issue went away).

        Returns the open findings representing this run (kept + newly
        created); suppressed fingerprints contribute nothing.
        """
        detected: dict[tuple, dict] = {}
        for spec in specs:
            fp = self.lint_fingerprint(
                spec.get("finding_type", ""),
                spec.get("title", ""),
                spec.get("related_page_ids"),
            )
            detected.setdefault(fp, spec)

        rows = self._db.execute(
            "SELECT * FROM wiki_lint_findings WHERE vault_id = ?", (vault_id,)
        ).fetchall()
        existing_by_fp: dict[tuple, list] = {}
        for row in rows:
            existing_by_fp.setdefault(self._row_lint_fingerprint(row), []).append(row)

        now = datetime.utcnow().isoformat()
        result: list = []
        with self.transaction():
            for fp, spec in detected.items():
                matches = existing_by_fp.get(fp)
                if matches:
                    open_rows = [r for r in matches if r["status"] == "open"]
                    if open_rows:
                        # Still open — keep the existing row as-is.
                        result.append(_to_lint_finding(open_rows[0]))
                    # else: only terminal (dismissed/resolved/acknowledged)
                    # rows share this fingerprint — suppressed on purpose.
                    continue
                result.append(
                    self._insert_lint_finding_row(
                        vault_id=vault_id,
                        finding_type=spec.get("finding_type", ""),
                        title=spec.get("title", ""),
                        severity=spec.get("severity", "medium"),
                        details=spec.get("details", ""),
                        related_page_ids=spec.get("related_page_ids"),
                        related_claim_ids=spec.get("related_claim_ids"),
                        commit=False,
                    )
                )
            for row in rows:
                if row["status"] == "open" and self._row_lint_fingerprint(row) not in detected:
                    self._db.execute(
                        "UPDATE wiki_lint_findings SET status = 'resolved', updated_at = ? "
                        "WHERE id = ? AND vault_id = ?",
                        (now, row["id"], vault_id),
                    )
        return result

    # -----------------------------------------------------------------------
    # Global Search
    # -----------------------------------------------------------------------

    def _filter_ids_in_chunks(
        self,
        table: str,
        ids: list[int],
        vault_id: int,
        extra_clauses: str = "",
        extra_params: Optional[list] = None,
    ) -> list[int]:
        """Filter candidate row ids in SQL, chunking the ``IN`` list.

        SEARCH-003 (#515): runs over the FULL FTS candidate set BEFORE any
        LIMIT so the visible cap applies after filtering. The chunking only
        keeps the host-parameter count under SQLite's default 999-variable
        limit; ``table`` / ``extra_clauses`` are internal constants, never
        user input.
        """
        matched: list[int] = []
        params = extra_params or []
        for start in range(0, len(ids), self._SQL_IN_CHUNK):
            chunk = ids[start : start + self._SQL_IN_CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = self._db.execute(
                f"SELECT id FROM {table} WHERE id IN ({ph}) AND vault_id = ?{extra_clauses}",
                [*chunk, vault_id, *params],
            ).fetchall()
            matched.extend(r[0] for r in rows)
        return matched

    def search(
        self,
        vault_id: int,
        query: str,
        limit: int = 20,
        page_type: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """Vault-wide search across pages, claims, and entities.

        SEARCH-003 (#515): the FTS id preselection only bounds the candidate
        pool (bm25-ranked, capped at ``FTS_CANDIDATE_CAP``). Filtering and the
        requested ordering run in SQL over the full candidate set, and LIMIT
        is applied last — a matching row inserted late can never be dropped
        by a pre-cap slice, and the FTS rank order never decides visible
        order.
        """
        page_ids = self._fts_page_ids(vault_id, query)
        claim_ids = self._fts_claim_ids(vault_id, query)
        entity_ids = self._fts_entity_ids(vault_id, query)

        pages = []
        if page_ids:
            filter_clauses = ""
            filter_params: list[Any] = []
            if page_type:
                filter_clauses += " AND page_type = ?"
                filter_params.append(page_type)
            if status:
                filter_clauses += " AND status = ?"
                filter_params.append(status)
            matched = self._filter_ids_in_chunks(
                "wiki_pages", page_ids, vault_id, filter_clauses, filter_params
            )
            if matched:
                allowed_sorts = {"updated_at", "created_at", "title", "confidence"}
                order_col = sort_by if sort_by in allowed_sorts else "updated_at"
                order_dir = "ASC" if order_col == "title" else "DESC"
                ph = ",".join("?" * len(matched))
                rows = self._db.execute(
                    f"SELECT * FROM wiki_pages WHERE id IN ({ph}) "
                    f"ORDER BY {order_col} {order_dir} LIMIT ?",
                    [*matched, limit],
                ).fetchall()
                pages = [_to_wiki_page(r) for r in rows]

        claims = []
        if claim_ids:
            matched = self._filter_ids_in_chunks("wiki_claims", claim_ids, vault_id)
            if matched:
                ph = ",".join("?" * len(matched))
                rows = self._db.execute(
                    f"SELECT * FROM wiki_claims WHERE id IN ({ph}) "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    [*matched, limit],
                ).fetchall()
                claims = [_to_wiki_claim(r) for r in rows]

        entities = []
        if entity_ids:
            matched = self._filter_ids_in_chunks("wiki_entities", entity_ids, vault_id)
            if matched:
                ph = ",".join("?" * len(matched))
                rows = self._db.execute(
                    f"SELECT * FROM wiki_entities WHERE id IN ({ph}) "
                    "ORDER BY canonical_name LIMIT ?",
                    [*matched, limit],
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
        """List version history for a page, most recent first.

        WIKI-003 (#515): versions saved within the same ``created_at`` second
        tie-break on descending insertion id (autoincrement, so id DESC is the
        insertion-order tie breaker). Without it, ``limit=1`` returned an
        arbitrary same-timestamp sibling — usually the OLDEST version.
        """
        rows = self._db.execute(
            "SELECT * FROM wiki_page_versions WHERE page_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
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
        """List files attached to a wiki page.

        AC40 (#515): LEFT JOINs ``files`` so each row carries the attachment's
        ``filename`` for display; legacy keys (id/page_id/file_id/vault_id/
        created_at) are unchanged.
        """
        rows = self._db.execute(
            """SELECT pf.*, f.file_name AS filename
               FROM wiki_page_files pf
               LEFT JOIN files f ON f.id = pf.file_id
               WHERE pf.page_id = ?
               ORDER BY pf.created_at""",
            (page_id,),
        ).fetchall()
        return [_to_page_file(r) for r in rows]

    def get_page_by_file(self, vault_id: int, file_id: int) -> Optional[WikiPage]:
        """Return the page a file is associated with in ``wiki_page_files``.

        WIKI-009 (#515): document-page identity on recompile must be
        resolved through the page-file association (stable per file), not
        through a slug derived only from the file_name — two different
        files sharing a name would otherwise collide onto one page. If a
        file is (exceptionally) associated with several pages, the oldest
        association wins for determinism.
        """
        row = self._db.execute(
            "SELECT page_id FROM wiki_page_files WHERE vault_id = ? AND file_id = ? "
            "ORDER BY id LIMIT 1",
            (vault_id, file_id),
        ).fetchone()
        if not row:
            return None
        return self.get_page(row[0], load_relations=False)

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
        """List pages that link TO this page (scoped to the page's vault).

        AC40 (#515): LEFT JOINs ``wiki_pages`` on the SOURCE page so each row
        carries the linking page's ``source_title`` / ``source_slug`` for
        display; legacy keys are unchanged.
        """
        rows = self._db.execute(
            """SELECT pl.*, wp.title AS source_title, wp.slug AS source_slug
               FROM wiki_page_links pl
               LEFT JOIN wiki_pages wp ON wp.id = pl.source_page_id
               WHERE pl.target_page_id = ? AND pl.vault_id = ?
               ORDER BY pl.created_at DESC""",
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
