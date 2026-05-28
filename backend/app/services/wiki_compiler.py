"""
WikiCompiler: Deterministic entity/acronym/relation extraction and memory promotion.

All deterministic extraction is regex-based. No LLM required for the
trust boundary. The optional LLM Wiki Curator (PR C) runs *after* the
deterministic pass and can only widen the candidate set — it can never
overrule a deterministic claim, and its output only becomes an active
wiki claim when its source_quote verifies against the chunk it cites.
"""

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings as _app_settings
from app.services.wiki_curator import (
    CuratorChunk,
    WikiCurator,
    deterministic_dedupe_key,
)
from app.services.wiki_curator_client import CuratorClient
from app.services.wiki_store import WikiStore, normalize_slug

# Approximate bytes per text chunk for deterministic chunk-uid generation.
# Matches the 2 000-character window used by SemanticChunker.
_COMPILE_CHUNK_SIZE = 2000

# Matches [S#], [M#], [W#] citation markers produced by the chat engine.
_CITE_STRIP_RE = re.compile(r"\[(?:S|M|W)\d+\]")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# "ACRONYM stands for Full Name" — e.g., "AFOMIS stands for Air Force..."
_ACRONYM_RE = re.compile(
    r"([A-Z][A-Z0-9]{1,})\s+stands\s+for\s+([A-Za-z][^.;!?\n]{3,})",
    re.IGNORECASE,
)

# "[Person] is the [ORG] [Role]" — e.g., "Justice Sakyi is the AFOMIS Chief"
# Group 2 is uppercase-only (acronym) so greedy matching can't bleed into adjacent clauses.
# No IGNORECASE: "is" and "the" are always lowercase in prose; role keywords keep exact case.
_ROLE_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+is\s+(?:the\s+)?([A-Z]{2,})\s+"
    r"(Chief|Deputy|Director|Lead|Head|Manager|Officer|Coordinator|Supervisor|Chair|President|VP|Secretary|Administrator|Analyst|Specialist|Engineer)",
)

# "[Person] is [his/her/their/its] [Role]" — pronoun-subject pattern
# "Major Justin Woods is his deputy"
_PRONOUN_ROLE_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+is\s+(?:his|her|their|its)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)",
    re.IGNORECASE,
)

# "[Person] is [Org]'s [Role]" — possessive org pattern
_ORG_POSSESSIVE_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+is\s+([A-Z][A-Z0-9]{1,})'s\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)",
    re.IGNORECASE,
)

# Pronouns that indicate subject coreference — skip as standalone subjects
_PRONOUN_SUBJECT_RE = re.compile(r"^(?:he|she|they|it|his|her|their|its)\b", re.IGNORECASE)

# Title prefixes to normalize person names
_TITLE_PREFIXES = re.compile(
    r"^(?:Major|Colonel|Captain|General|Dr\.?|Mr\.?|Ms\.?|Mrs\.?|Lt\.?|Sgt\.?|Cpl\.?)\s+",
    re.IGNORECASE,
)


def _strip_title(name: str) -> str:
    return _TITLE_PREFIXES.sub("", name).strip()


def _extract_name_with_title(raw: str) -> str:
    """Return name with title preserved for display."""
    return raw.strip()


# ---------------------------------------------------------------------------
# Extraction result types
# ---------------------------------------------------------------------------

class ExtractionResult:
    def __init__(self):
        self.acronyms: list[dict] = []      # {acronym, full_name}
        self.persons: list[str] = []         # canonical names
        self.role_claims: list[dict] = []    # {subject, predicate, object_org, sentence}
        self.sentences_skipped: list[str] = []


def extract_entities_from_text(text: str) -> ExtractionResult:
    """
    Deterministically extract entities, acronyms, and role relations from text.

    Pronoun handling:
    - Tracks current_org_context (most recently extracted acronym entity).
    - When a pronoun-subject sentence is detected, uses current_org_context as
      the implied org if available.
    - Uses finditer for ROLE_RE and PRONOUN_ROLE_RE so compound sentences with
      multiple clauses (joined by "and") are handled correctly.
    """
    result = ExtractionResult()
    current_org_context: Optional[str] = None
    # (subject, predicate, object_person) tuples to avoid duplicates
    _seen: set[tuple] = set()

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # 1. Acronym extraction: "X stands for Y"
        acronym_match = _ACRONYM_RE.search(sentence)
        if acronym_match:
            acronym = acronym_match.group(1).upper()
            full_name = acronym_match.group(2).strip().rstrip(".")
            result.acronyms.append({"acronym": acronym, "full_name": full_name})
            current_org_context = acronym
            logger.debug("Extracted acronym: %s = %s", acronym, full_name)
            # Continue: acronym sentences rarely contain role patterns.
            continue

        # 2. Org possessive: "Person is ORG's Role" — finditer for all matches
        for poss_match in _ORG_POSSESSIVE_RE.finditer(sentence):
            person = _extract_name_with_title(poss_match.group(1))
            org = poss_match.group(2).upper()
            role = poss_match.group(3).strip().lower()
            key = (org, role, person)
            if key not in _seen:
                _seen.add(key)
                result.persons.append(person)
                result.role_claims.append({
                    "subject": org, "predicate": role,
                    "object_person": person, "sentence": sentence,
                })
                logger.debug("Extracted possessive role: %s -[%s]-> %s", org, role, person)

        # 3. Explicit role with org acronym: "[Person] is the [ORG] [Role]"
        # Uses finditer so multiple role clauses in one sentence are all captured.
        for role_match in _ROLE_RE.finditer(sentence):
            person = _extract_name_with_title(role_match.group(1))
            org = role_match.group(2).strip()  # already uppercase-only by regex
            role = role_match.group(3).strip().lower()
            key = (org, role, person)
            if key not in _seen:
                _seen.add(key)
                result.persons.append(person)
                result.role_claims.append({
                    "subject": org, "predicate": role,
                    "object_person": person, "sentence": sentence,
                })
                logger.debug("Extracted role: %s -[%s]-> %s", org, role, person)

        # 4. Pronoun-subject role: "[Person] is his/her/their [Role]"
        # Also uses finditer; dedup prevents double-counting overlap with ROLE_RE.
        for pronoun_match in _PRONOUN_ROLE_RE.finditer(sentence):
            person = _extract_name_with_title(pronoun_match.group(1))
            role = pronoun_match.group(2).strip().lower()
            if current_org_context:
                key = (current_org_context, role, person)
                if key not in _seen:
                    _seen.add(key)
                    result.persons.append(person)
                    result.role_claims.append({
                        "subject": current_org_context, "predicate": role,
                        "object_person": person, "sentence": sentence,
                    })
                    logger.debug(
                        "Pronoun resolved via org_context=%s: %s -[%s]-> %s",
                        current_org_context, current_org_context, role, person,
                    )
            else:
                result.sentences_skipped.append(sentence)
                logger.debug("Skipped pronoun sentence (no org context): %s", sentence)

    return result


# ---------------------------------------------------------------------------
# WikiCompiler
# ---------------------------------------------------------------------------

class WikiCompiler:
    """
    Orchestrates deterministic extraction and wiki page/claim/entity creation.

    Instantiate per-request: WikiCompiler(db) or WikiCompiler(db, wiki_store).
    """

    def __init__(self, db: sqlite3.Connection, store: Optional[WikiStore] = None) -> None:
        self._db = db
        self._store = store or WikiStore(db)

    def _find_or_create_claim(
        self,
        vault_id: int,
        claim_text: str,
        source_kind: str,
        source_identity: dict,
        **create_kwargs,
    ) -> tuple:
        """Return (claim, created). If claim exists, attach missing source; otherwise create.

        source_identity is used to detect duplicate sources; keys vary by kind:
          document → file_id (int)
          memory   → memory_id (int)
          manual   → source_label (str)
        """
        # Exact match first; fall back to normalized (fuzzy) match so claims that
        # differ only in punctuation/whitespace/case are deduped (DD-C011).
        existing = self._store.find_claim_by_text(vault_id, claim_text) or \
            self._store.find_claim_by_normalized_text(vault_id, claim_text)
        if existing:
            # Promote unverified → active when the caller has explicit per-claim citations.
            new_status = create_kwargs.get("status")
            if existing.status == "unverified" and new_status == "active":
                self._db.execute(
                    "UPDATE wiki_claims SET status = 'active', source_type = ?, confidence = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (
                        create_kwargs.get("source_type", existing.source_type),
                        create_kwargs.get("confidence", existing.confidence),
                        existing.id,
                    ),
                )
                self._db.commit()
            # Attach dedup source if not already present
            already = any(
                s.source_kind == source_kind and self._source_matches(s, source_kind, source_identity)
                for s in existing.sources
            )
            if not already:
                self._store.attach_source(
                    claim_id=existing.id,
                    source_kind=source_kind,
                    **source_identity,
                    **{k: v for k, v in create_kwargs.items() if k in (
                        "quote", "confidence", "chunk_id", "source_label"
                    )},
                )
                self._db.commit()
            return existing, False
        # create_claim doesn't accept source-level fields; strip them before forwarding
        _claim_fields = {
            k: v for k, v in create_kwargs.items()
            if k not in ("quote", "chunk_id")
        }
        claim = self._store.create_claim(
            vault_id=vault_id,
            claim_text=claim_text,
            **_claim_fields,
        )
        return claim, True

    @staticmethod
    def _source_matches(source, kind: str, identity: dict) -> bool:
        if kind == "document":
            return source.file_id == identity.get("file_id")
        if kind == "memory":
            return source.memory_id == identity.get("memory_id")
        return source.source_label == identity.get("source_label")

    def _find_or_create_relation(
        self,
        vault_id: int,
        predicate: str,
        subject_entity_id,
        object_entity_id,
        **create_kwargs,
    ):
        """Return existing relation or create a new one."""
        existing = self._store.find_relation(vault_id, predicate, subject_entity_id, object_entity_id)
        if existing:
            return existing
        return self._store.create_relation(
            vault_id=vault_id,
            predicate=predicate,
            subject_entity_id=subject_entity_id,
            object_entity_id=object_entity_id,
            **create_kwargs,
        )

    def promote_memory(
        self,
        memory_id: int,
        vault_id: int,
        page_type: Optional[str] = None,
        target_page_id: Optional[int] = None,
        status: str = "needs_review",
        created_by: Optional[int] = None,
    ) -> dict:
        """
        Promote a memory record into wiki pages, entities, claims, and relations.

        Returns dict with keys: page, claims, entities, relations.
        """
        # Load memory — FK verify vault scope
        self._db.row_factory = sqlite3.Row
        row = self._db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            raise ValueError(f"Memory {memory_id} not found")

        memory = dict(row)
        memory_vault_id = memory.get("vault_id")

        # Vault scope enforcement
        if memory_vault_id is not None and memory_vault_id != vault_id:
            raise PermissionError(
                f"Memory {memory_id} belongs to vault {memory_vault_id}, not {vault_id}"
            )

        content: str = memory["content"]
        extraction = extract_entities_from_text(content)

        entities_created: list = []
        claims_created: list = []
        relations_created: list = []

        # Determine primary acronym for page slug
        primary_acronym = extraction.acronyms[0]["acronym"] if extraction.acronyms else None
        inferred_page_type = page_type or ("acronym" if primary_acronym else "entity")

        # Determine or create page
        if target_page_id:
            page = self._store.get_page(target_page_id)
            if not page or page.vault_id != vault_id:
                raise ValueError(f"Target page {target_page_id} not found in vault {vault_id}")
        else:
            if primary_acronym:
                slug = normalize_slug(f"acronym/{primary_acronym.lower()}")
                title = primary_acronym
            else:
                slug = normalize_slug(content[:60])
                title = content[:80].strip()
            # Check for existing page with same slug
            existing_row = self._db.execute(
                "SELECT id FROM wiki_pages WHERE vault_id = ? AND slug = ?", (vault_id, slug)
            ).fetchone()
            if existing_row:
                page = self._store.get_page(existing_row[0], load_relations=False)
            else:
                page = self._store.create_page(
                    vault_id=vault_id,
                    title=title,
                    page_type=inferred_page_type,
                    slug=slug,
                    markdown=content,
                    status=status,
                    created_by=created_by,
                )

        page_id = page.id  # type: ignore[union-attr]

        # Create/upsert acronym entities
        for acronym_info in extraction.acronyms:
            entity = self._store.upsert_entity(
                vault_id=vault_id,
                canonical_name=acronym_info["acronym"],
                entity_type="acronym",
                aliases=[acronym_info["full_name"]],
                page_id=page_id,
            )
            entities_created.append(entity)

        # Create person entities
        seen_persons: set[str] = set()
        person_entity_map: dict[str, int] = {}
        for person_name in extraction.persons:
            if person_name in seen_persons:
                continue
            seen_persons.add(person_name)
            entity = self._store.upsert_entity(
                vault_id=vault_id,
                canonical_name=person_name,
                entity_type="person",
            )
            entities_created.append(entity)
            person_entity_map[person_name] = entity.id

        # Build entity id map for relations
        entity_id_map: dict[str, int] = {}
        for e in entities_created:
            entity_id_map[e.canonical_name] = e.id

        # Create claims and relations from role_claims (idempotent)
        for role_claim in extraction.role_claims:
            subj_name = role_claim["subject"]
            pred = role_claim["predicate"]
            obj_name = role_claim["object_person"]
            sentence = role_claim["sentence"]

            claim, _created = self._find_or_create_claim(
                vault_id=vault_id,
                claim_text=sentence,
                source_kind="memory",
                source_identity={"memory_id": memory_id, "source_label": f"memory:{memory_id}"},
                source_type="memory",
                page_id=page_id,
                claim_type="fact",
                subject=subj_name,
                predicate=pred,
                object=obj_name,
                status="active",
                confidence=0.8,
                created_by=created_by,
                quote=sentence,
            )
            if _created:
                self._store.attach_source(
                    claim_id=claim.id,
                    source_kind="memory",
                    memory_id=memory_id,
                    source_label=f"memory:{memory_id}",
                    quote=content,
                    confidence=0.8,
                )
                self._db.commit()
            claims_created.append(self._store.get_claim(claim.id))

            # Create relation if both entities are known (idempotent)
            subj_id = entity_id_map.get(subj_name)
            obj_id = entity_id_map.get(obj_name)
            relation = self._find_or_create_relation(
                vault_id=vault_id,
                predicate=pred,
                subject_entity_id=subj_id,
                object_entity_id=obj_id,
                object_text=obj_name if not obj_id else None,
                claim_id=claim.id,
                confidence=0.8,
            )
            relations_created.append(relation)

        # Reload page with all relations
        page = self._store.get_page(page_id)

        # PR C: optional curator on the manual / promote_memory path.
        # Gated by wiki_llm_curator_run_on_manual.
        curator_summary = self._maybe_run_curator(
            vault_id=vault_id,
            file_id=None,
            text=content,
            trigger="manual",
            deterministic_extraction=extraction,
            page_id=page_id,
            existing_entities=entities_created,
        )

        result_dict: dict = {
            "page": page,
            "claims": claims_created,
            "entities": entities_created,
            "relations": relations_created,
        }
        if curator_summary is not None:
            result_dict["curator"] = curator_summary
        return result_dict

    def compile_query_job(self, vault_id: int, input_json: dict) -> dict:
        """
        Extract entities/claims from an assistant answer and persist to wiki.

        Per-claim citation attribution is driven by the optional ``per_claim_sources``
        field in input_json, which maps sentence text → list of source dicts.
        Each source dict must have at minimum a ``source_kind`` key.

        When ``per_claim_sources`` is present:
          - Claims that appear in the map with non-empty sources → status='active'
          - Claims with no entry or empty sources → status='unverified' + lint finding

        When ``per_claim_sources`` is absent (legacy / answer-level citations):
          - All claims default to status='unverified'
          - Answer-level refs (wiki_refs, doc_sources, memories) are attached for
            context, but status stays 'unverified'

        source_type is derived per-claim from the kinds of sources actually cited.
        """
        assistant_answer: str = input_json.get("assistant_answer") or ""
        # Per-claim source mapping: {"sentence text": [source_dicts]}
        per_claim_sources: dict = input_json.get("per_claim_sources") or {}
        # Answer-level fallback refs (backward compat)
        wiki_refs: list = input_json.get("wiki_refs") or []
        doc_sources: list = input_json.get("doc_sources") or []
        memories: list = input_json.get("memories") or []

        if not assistant_answer:
            return {"page": None, "claims": [], "entities": [], "relations": [], "skipped": True}

        # Strip citation markers so claim text is citation-free and sentence keys
        # match the citation-stripped keys produced by _build_per_claim_sources.
        _clean_answer = _CITE_STRIP_RE.sub("", assistant_answer)
        # Remove stray spaces before punctuation (e.g., "Claim ." → "Claim.").
        _clean_answer = re.sub(r"\s+([.!?,;:])", r"\1", _clean_answer)
        _clean_answer = re.sub(r"\s{2,}", " ", _clean_answer).strip()
        extraction = extract_entities_from_text(_clean_answer)
        if not extraction.acronyms and not extraction.role_claims:
            return {"page": None, "claims": [], "entities": [], "relations": [], "skipped": True}

        entities_created: list = []
        claims_created: list = []
        relations_created: list = []

        for acronym_info in extraction.acronyms:
            entity = self._store.upsert_entity(
                vault_id=vault_id,
                canonical_name=acronym_info["acronym"],
                entity_type="acronym",
                aliases=[acronym_info["full_name"]],
            )
            entities_created.append(entity)

        seen_persons: set[str] = set()
        for person_name in extraction.persons:
            if person_name in seen_persons:
                continue
            seen_persons.add(person_name)
            entity = self._store.upsert_entity(
                vault_id=vault_id,
                canonical_name=person_name,
                entity_type="person",
            )
            entities_created.append(entity)

        entity_id_map: dict[str, int] = {e.canonical_name: e.id for e in entities_created}
        now = datetime.utcnow().isoformat()

        for role_claim in extraction.role_claims:
            subj_name = role_claim["subject"]
            pred = role_claim["predicate"]
            obj_name = role_claim["object_person"]
            sentence = role_claim["sentence"]

            # Determine which sources are cited for this specific claim
            if per_claim_sources:
                claim_citations: list = per_claim_sources.get(sentence) or []
            else:
                # Legacy: answer-level sources — no per-sentence attribution available
                claim_citations = []

            has_specific_citations = bool(claim_citations)
            claim_status = "active" if has_specific_citations else "unverified"
            confidence = 0.8 if has_specific_citations else 0.5

            # Derive source_type from the kinds of sources actually cited for this claim
            cited_kinds = {s.get("source_kind") for s in claim_citations if s.get("source_kind")}
            if not cited_kinds:
                source_type = "chat_synthesis"
            elif len(cited_kinds) > 1:
                source_type = "mixed"
            elif "document" in cited_kinds:
                source_type = "document"
            elif "memory" in cited_kinds:
                source_type = "memory"
            else:
                source_type = "chat_synthesis"

            # For dedup identity, use the first citation's source_kind+id if available
            if has_specific_citations:
                first_src = claim_citations[0]
                first_kind = first_src.get("source_kind", "manual")
                if first_kind == "document":
                    dedup_kind = "document"
                    dedup_identity = {"file_id": first_src.get("file_id"), "source_label": first_src.get("source_label", "")}
                elif first_kind == "memory":
                    dedup_kind = "memory"
                    dedup_identity = {"memory_id": first_src.get("memory_id"), "source_label": first_src.get("source_label", "")}
                else:
                    dedup_kind = "manual"
                    dedup_identity = {"source_label": first_src.get("source_label", first_src.get("wiki_label", ""))}
            else:
                dedup_kind = "manual"
                dedup_identity = {"source_label": "chat_synthesis"}

            claim, _created = self._find_or_create_claim(
                vault_id=vault_id,
                claim_text=sentence,
                source_kind=dedup_kind,
                source_identity=dedup_identity,
                source_type=source_type,
                claim_type="fact",
                subject=subj_name,
                predicate=pred,
                object=obj_name,
                status=claim_status,
                confidence=confidence,
                quote=sentence,
            )

            if has_specific_citations:
                # Attach ALL per-claim citations for both new and existing claims.
                # For existing claims, reload sources from DB so the snapshot includes
                # any source that _find_or_create_claim just attached (its return value
                # is stale after an inline attach+commit).
                if not _created:
                    _refreshed = self._store.find_claim_by_text(vault_id, sentence)
                    _current_sources = list(
                        (_refreshed.sources if _refreshed else None) or []
                    )
                else:
                    _current_sources = []
                for src in claim_citations:
                    kind = src.get("source_kind", "manual")
                    if kind == "document":
                        src_identity = {"file_id": src.get("file_id")}
                    elif kind == "memory":
                        src_identity = {"memory_id": src.get("memory_id")}
                    else:
                        src_identity = {
                            "source_label": src.get("source_label", src.get("wiki_label", ""))
                        }
                    _already = any(
                        s.source_kind == kind and self._source_matches(s, kind, src_identity)
                        for s in _current_sources
                    )
                    if not _already:
                        if kind == "document":
                            self._store.attach_source(
                                claim_id=claim.id,
                                source_kind="document",
                                file_id=src.get("file_id"),
                                chunk_id=src.get("chunk_id"),
                                source_label=src.get("source_label", ""),
                                quote=sentence,
                                confidence=confidence,
                            )
                        elif kind == "memory":
                            self._store.attach_source(
                                claim_id=claim.id,
                                source_kind="memory",
                                memory_id=src.get("memory_id"),
                                source_label=src.get("source_label", ""),
                                quote=sentence,
                                confidence=confidence,
                            )
                        else:
                            self._store.attach_source(
                                claim_id=claim.id,
                                source_kind="manual",
                                source_label=src.get("source_label", src.get("wiki_label", "")),
                                quote=sentence,
                                confidence=confidence,
                            )
                        # Track inline to prevent double-attaching within this loop.
                        _current_sources.append(
                            type("_S", (), {"source_kind": kind, **src_identity})()
                        )
            elif _created:
                # No per-claim citations: attach answer-level refs for context only.
                for ref in wiki_refs:
                    if ref.get("wiki_label"):
                        self._store.attach_source(
                            claim_id=claim.id,
                            source_kind="manual",
                            source_label=ref["wiki_label"],
                            quote=sentence,
                            confidence=confidence,
                        )
                for src in doc_sources:
                    if src.get("source_label"):
                        self._store.attach_source(
                            claim_id=claim.id,
                            source_kind="document",
                            file_id=src.get("file_id"),
                            chunk_id=src.get("chunk_id"),
                            source_label=src["source_label"],
                            quote=sentence,
                            confidence=confidence,
                        )
                for mem in memories:
                    if isinstance(mem, dict) and mem.get("memory_label"):
                        self._store.attach_source(
                            claim_id=claim.id,
                            source_kind="memory",
                            memory_id=mem.get("memory_id"),
                            source_label=mem["memory_label"],
                            quote=sentence,
                            confidence=confidence,
                        )

            self._db.commit()
            claims_created.append(self._store.get_claim(claim.id))

            # Lint finding for every unverified claim (only on first creation)
            if _created and claim_status == "unverified":
                self._db.execute(
                    """INSERT INTO wiki_lint_findings
                       (vault_id, finding_type, severity, title, details,
                        related_page_ids_json, related_claim_ids_json, status, created_at, updated_at)
                       VALUES (?, 'unsupported_claim', 'low', ?, ?, '[]', ?, 'open', ?, ?)""",
                    (
                        vault_id,
                        "Unverified claim: extracted from query answer with no per-claim citations",
                        sentence[:200],
                        json.dumps([claim.id]),
                        now, now,
                    ),
                )
                self._db.commit()

            subj_id = entity_id_map.get(subj_name)
            obj_id = entity_id_map.get(obj_name)
            relation = self._find_or_create_relation(
                vault_id=vault_id,
                predicate=pred,
                subject_entity_id=subj_id,
                object_entity_id=obj_id,
                object_text=obj_name if not obj_id else None,
                claim_id=claim.id,
                confidence=confidence,
            )
            relations_created.append(relation)

        # PR C: optional curator on query path. Default-disabled
        # (wiki_llm_curator_run_on_query=False) to protect chat latency,
        # but operators who flip it on get the same provenance gating
        # as the ingest path. file_id is None here — curator chunks
        # therefore use a 'text_N' chunk_id prefix.
        curator_summary = self._maybe_run_curator(
            vault_id=vault_id,
            file_id=None,
            text=_clean_answer,
            trigger="query",
            deterministic_extraction=extraction,
            page_id=None,
            existing_entities=entities_created,
        )

        result_dict: dict = {
            "page": None,
            "claims": [{"id": c.id, "status": c.status} for c in claims_created],
            "entities": [{"id": e.id, "name": e.canonical_name} for e in entities_created],
            "relations_count": len(relations_created),
        }
        if curator_summary is not None:
            result_dict["curator"] = curator_summary
        return result_dict

    def compile_ingest_job(self, vault_id: int, input_json: dict) -> dict:
        """
        Extract entities/claims from a document and persist to wiki.

        input_json must contain 'file_id'. If 'text' is present it is used
        directly; otherwise the method reads from the file path recorded in
        the files table.
        """
        file_id: Optional[int] = input_json.get("file_id")
        if not file_id:
            return {"page": None, "claims": [], "entities": [], "relations_count": 0, "skipped": True}

        self._db.row_factory = sqlite3.Row
        file_row = self._db.execute(
            "SELECT * FROM files WHERE id = ? AND vault_id = ?", (file_id, vault_id)
        ).fetchone()
        if not file_row:
            raise ValueError(f"File {file_id} not found in vault {vault_id}")

        file_data = dict(file_row)
        # Priority: explicit text in job → durable parsed_text → re-parse from file_path.
        text: str = input_json.get("text") or file_data.get("parsed_text") or ""

        if not text:
            _file_path = file_data.get("file_path") or ""
            if _file_path:
                # Attempt 1: full parser stack (requires unstructured.io).
                try:
                    from app.services.document_processor import DocumentParser
                    _elements = DocumentParser().parse(_file_path)
                    text = "\n".join(str(e) for e in _elements if str(e).strip())
                except Exception as _exc:
                    logger.debug(
                        "compile_ingest_job: DocumentParser failed for file_id=%d: %s",
                        file_id, _exc,
                    )
                # Attempt 2: plain UTF-8 read for .txt files (test-safe fallback).
                if not text and _file_path.lower().endswith(".txt"):
                    try:
                        text = Path(_file_path).read_text(encoding="utf-8", errors="replace")
                        # Replacement chars (U+FFFD) indicate binary content masquerading
                        # as text. Discard the garbage to avoid caching unusable content.
                        if text and "�" in text:
                            logger.debug(
                                "compile_ingest_job: binary content via replacement chars, "
                                "discarding .txt fallback for file_id=%d",
                                file_id,
                            )
                            text = ""
                    except Exception as _exc:
                        logger.debug(
                            "compile_ingest_job: plain-text fallback failed for file_id=%d: %s",
                            file_id, _exc,
                        )
                if text:
                    # Cache for future compiles so re-parse is a one-time cost.
                    self._db.execute(
                        "UPDATE files SET parsed_text = ? WHERE id = ?", (text, file_id)
                    )
                    self._db.commit()

        if not text:
            logger.warning(
                "compile_ingest_job: no text available for file_id=%d "
                "(input_json['text'], files.parsed_text, and file re-parse all empty)",
                file_id,
            )
            return {"page": None, "claims": [], "entities": [], "relations_count": 0, "skipped": True}

        extraction = extract_entities_from_text(text)
        # Always materialize a page for the document so the wiki reflects every
        # ingested file. Deterministic claim/entity/relation passes below are
        # no-ops when extraction is empty; the optional LLM curator can still
        # populate claims later. Prior behavior short-circuited here and left
        # the wiki blank for any document whose prose did not match the narrow
        # acronym / ALL-CAPS-org role patterns.

        file_name = file_data.get("file_name") or f"file:{file_id}"
        slug = normalize_slug(f"document/{file_name[:60]}")
        title = file_name

        existing = self._db.execute(
            "SELECT id FROM wiki_pages WHERE vault_id = ? AND slug = ?", (vault_id, slug)
        ).fetchone()
        if existing:
            page = self._store.get_page(existing[0], load_relations=False)
        else:
            page = self._store.create_page(
                vault_id=vault_id,
                title=title,
                page_type="entity",
                slug=slug,
                markdown=text[:2000],
                status="needs_review",
            )

        page_id = page.id  # type: ignore[union-attr]

        entities_created: list = []
        claims_created: list = []
        relations_count = 0

        for acronym_info in extraction.acronyms:
            entity = self._store.upsert_entity(
                vault_id=vault_id,
                canonical_name=acronym_info["acronym"],
                entity_type="acronym",
                aliases=[acronym_info["full_name"]],
                page_id=page_id,
            )
            entities_created.append(entity)

        seen_persons: set[str] = set()
        for person_name in extraction.persons:
            if person_name in seen_persons:
                continue
            seen_persons.add(person_name)
            entity = self._store.upsert_entity(
                vault_id=vault_id,
                canonical_name=person_name,
                entity_type="person",
            )
            entities_created.append(entity)

        entity_id_map: dict[str, int] = {e.canonical_name: e.id for e in entities_created}

        _last_pos = 0  # running offset so duplicate sentences get distinct chunk uids
        for role_claim in extraction.role_claims:
            subj_name = role_claim["subject"]
            pred = role_claim["predicate"]
            obj_name = role_claim["object_person"]
            sentence = role_claim["sentence"]

            # Deterministic chunk reference: position-based index into 2 000-char windows.
            # Use a running offset so repeated sentences advance past previous matches.
            # If not found from _last_pos (extraction order diverged from text order),
            # fall back to first occurrence rather than returning a wrong chunk index.
            _pos = text.find(sentence, _last_pos)
            if _pos < 0:
                _pos = text.find(sentence)  # first-occurrence fallback
            _chunk_uid = f"{file_id}_{_pos // _COMPILE_CHUNK_SIZE}" if _pos >= 0 else f"{file_id}_0"
            if _pos >= 0:
                _last_pos = _pos + len(sentence)

            claim, _created = self._find_or_create_claim(
                vault_id=vault_id,
                claim_text=sentence,
                source_kind="document",
                source_identity={"file_id": file_id, "source_label": f"file:{file_id}"},
                source_type="document",
                page_id=page_id,
                claim_type="fact",
                subject=subj_name,
                predicate=pred,
                object=obj_name,
                status="active",
                confidence=0.8,
                quote=sentence,
                chunk_id=_chunk_uid,
            )
            if _created:
                self._store.attach_source(
                    claim_id=claim.id,
                    source_kind="document",
                    file_id=file_id,
                    chunk_id=_chunk_uid,
                    source_label=f"file:{file_id}",
                    quote=sentence,
                    confidence=0.8,
                )
                self._db.commit()
            claims_created.append({"id": claim.id, "status": claim.status})

            subj_id = entity_id_map.get(subj_name)
            obj_id = entity_id_map.get(obj_name)
            self._find_or_create_relation(
                vault_id=vault_id,
                predicate=pred,
                subject_entity_id=subj_id,
                object_entity_id=obj_id,
                object_text=obj_name if not obj_id else None,
                claim_id=claim.id,
                confidence=0.7,
            )
            relations_count += 1

        # ----------- Optional LLM Wiki Curator (PR C) -----------
        # Gated by settings.wiki_llm_curator_enabled AND the per-trigger
        # flag. Curator output never replaces deterministic output; it
        # can only add candidates that pass quote+chunk verification.
        # Errors are recorded into the result, never raised.
        curator_summary = self._maybe_run_curator(
            vault_id=vault_id,
            file_id=file_id,
            text=text,
            trigger="ingest",
            deterministic_extraction=extraction,
            page_id=page_id,
            existing_entities=entities_created,
        )

        page = self._store.get_page(page_id)
        result_dict: dict = {
            "page": {"id": page_id, "slug": slug} if page else None,
            "claims": claims_created,
            "entities": [{"id": e.id, "name": e.canonical_name} for e in entities_created],
            "relations_count": relations_count,
        }
        if curator_summary is not None:
            result_dict["curator"] = curator_summary
        return result_dict

    # ------------------------------------------------------------------
    # Optional LLM Wiki Curator integration (PR C)
    # ------------------------------------------------------------------

    def _maybe_run_curator(
        self,
        *,
        vault_id: int,
        file_id: Optional[int],
        text: str,
        trigger: str,  # 'ingest' | 'query' | 'manual'
        deterministic_extraction,
        page_id: Optional[int],
        existing_entities: list,
    ) -> Optional[dict]:
        """Run the curator if enabled for this trigger; return summary
        dict for ``result_json.curator`` or None when curator is off.

        All exceptions are caught and recorded in the returned summary;
        a curator failure must NEVER fail the underlying compile job.
        """
        if not getattr(_app_settings, "wiki_llm_curator_enabled", False):
            return None
        flag_for_trigger = {
            "ingest": "wiki_llm_curator_run_on_ingest",
            "query": "wiki_llm_curator_run_on_query",
            "manual": "wiki_llm_curator_run_on_manual",
        }.get(trigger)
        if not flag_for_trigger or not getattr(
            _app_settings, flag_for_trigger, False
        ):
            return None
        url = getattr(_app_settings, "wiki_llm_curator_url", "") or ""
        model = getattr(_app_settings, "wiki_llm_curator_model", "") or ""
        if not url.strip() or not model.strip():
            return {
                "errors": ["disabled: missing url or model"],
                "accepted": 0,
                "rejected": 0,
                "lint": 0,
                "calls": 0,
                "input_chars": 0,
            }
        if not text:
            return None

        # Build chunks the same way the deterministic pass infers them.
        chunks = self._build_curator_chunks(text=text, file_id=file_id)
        if not chunks:
            return None

        # Seed dedupe with deterministic role_claims so the curator
        # can't re-emit the same fact as a duplicate "needs_review" row.
        dedupe_seed: set[str] = set()
        for rc in deterministic_extraction.role_claims:
            dedupe_seed.add(
                deterministic_dedupe_key(
                    rc.get("subject", ""),
                    rc.get("predicate", ""),
                    rc.get("object_person", ""),
                    rc.get("sentence", ""),
                )
            )

        deterministic_summary = {
            "acronyms": [
                {"acronym": a.get("acronym"), "full_name": a.get("full_name")}
                for a in deterministic_extraction.acronyms
            ],
            "role_claims": [
                {
                    "subject": rc.get("subject"),
                    "predicate": rc.get("predicate"),
                    "object": rc.get("object_person"),
                }
                for rc in deterministic_extraction.role_claims
            ],
        }
        existing_brief = [e.canonical_name for e in existing_entities[:50]]

        client = CuratorClient(
            base_url=url,
            model=model,
            timeout=float(getattr(_app_settings, "wiki_llm_curator_timeout_sec", 120.0)),
            max_tokens=int(getattr(_app_settings, "wiki_llm_curator_max_output_tokens", 2048)),
            temperature=float(getattr(_app_settings, "wiki_llm_curator_temperature", 0.0)),
        )
        curator = WikiCurator(client=client, store=self._store)

        try:
            # The compiler is sync, but CuratorClient.propose is async.
            # Use a fresh event loop only when there isn't one already
            # running (background processor calls us from inside one).
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None:
                cur_result = asyncio.run(
                    curator.curate(
                        vault_id=vault_id,
                        file_id=file_id,
                        chunks=chunks,
                        deterministic_summary=deterministic_summary,
                        existing_entities_brief=existing_brief,
                        deterministic_dedupe_keys=dedupe_seed,
                    )
                )
            else:
                # Schedule on the running loop and block until done.
                # We can't do `loop.run_until_complete` from inside the
                # loop, so spin a separate thread.
                import concurrent.futures

                def _runner():
                    return asyncio.run(
                        curator.curate(
                            vault_id=vault_id,
                            file_id=file_id,
                            chunks=chunks,
                            deterministic_summary=deterministic_summary,
                            existing_entities_brief=existing_brief,
                            deterministic_dedupe_keys=dedupe_seed,
                        )
                    )

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    cur_result = ex.submit(_runner).result()
        except Exception as e:  # broad — curator must never fail the job
            logger.warning(
                "wiki curator: orchestration failed for vault=%s file=%s: %s",
                vault_id, file_id, e,
            )
            return {
                "errors": [f"orchestration_error: {type(e).__name__}: {e}"],
                "accepted": 0,
                "rejected": 0,
                "lint": 0,
                "calls": 0,
                "input_chars": 0,
            }

        # Persist accepted candidates as wiki_claims with provenance.
        for accepted in cur_result.accepted:
            try:
                claim = self._store.create_claim(
                    vault_id=vault_id,
                    claim_text=accepted.claim_text,
                    source_type="document",
                    page_id=page_id,
                    claim_type=accepted.claim_type,
                    subject=accepted.subject,
                    predicate=accepted.predicate,
                    object=accepted.object,
                    status=accepted.status,
                    confidence=accepted.confidence,
                    created_by_kind="llm_curator",
                )
                self._store.attach_source(
                    claim_id=claim.id,
                    source_kind="document",
                    file_id=accepted.file_id,
                    chunk_id=accepted.chunk_id,
                    source_label=accepted.source_label or (
                        f"file:{accepted.file_id}" if accepted.file_id else "curator"
                    ),
                    quote=accepted.source_quote,
                    confidence=accepted.confidence,
                )
            except Exception as e:  # pragma: no cover - defensive
                cur_result.errors.append(
                    f"persist_error: {type(e).__name__}: {e}"
                )
                logger.warning(
                    "wiki curator: failed to persist accepted claim: %s", e
                )

        # Persist lint findings.
        for finding in cur_result.lint_findings:
            try:
                self._store.create_lint_finding(
                    vault_id=vault_id,
                    finding_type=finding["finding_type"],
                    severity=finding.get("severity", "medium"),
                    title=finding.get("title", "Curator lint"),
                    details=finding.get("details", ""),
                )
            except Exception as e:  # pragma: no cover - defensive
                cur_result.errors.append(
                    f"lint_persist_error: {type(e).__name__}: {e}"
                )

        return cur_result.to_summary()

    @staticmethod
    def _build_curator_chunks(
        *, text: str, file_id: Optional[int]
    ) -> list[CuratorChunk]:
        """Slice the source text into 2 000-char windows so the curator's
        chunk_id values match the deterministic ``f"{file_id}_{idx}"``
        format used elsewhere in the compiler."""
        if not text:
            return []
        out: list[CuratorChunk] = []
        max_input = int(getattr(_app_settings, "wiki_llm_curator_max_input_chars", 6000))
        budget = max(_COMPILE_CHUNK_SIZE, max_input)
        # Take from the start of the text up to budget; bounded by the
        # curator's own per-call max_input_chars, so we don't need to
        # paginate here.
        bound_text = text[:budget]
        for i in range(0, len(bound_text), _COMPILE_CHUNK_SIZE):
            window = bound_text[i : i + _COMPILE_CHUNK_SIZE]
            chunk_id = (
                f"{file_id}_{i // _COMPILE_CHUNK_SIZE}"
                if file_id is not None
                else f"text_{i // _COMPILE_CHUNK_SIZE}"
            )
            out.append(
                CuratorChunk(
                    chunk_id=chunk_id,
                    source_text=window,
                    file_id=file_id,
                    source_label=(
                        f"file:{file_id}" if file_id is not None else None
                    ),
                )
            )
        return out
