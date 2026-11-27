"""
WikiCompiler: Deterministic entity/acronym/relation extraction and memory promotion.

All deterministic extraction is regex-based. No LLM required for the
trust boundary. The optional LLM Wiki Curator (PR C) runs *after* the
deterministic pass and can only widen the candidate set — it can never
overrule a deterministic claim, and its output only becomes an active
wiki claim when its source_quote verifies against the chunk it cites.
"""

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from app.config import settings as _app_settings
from app.services.content_classifiers import (
    Procedure,
    content_text,
    entry_summary,
    extract_procedures,
)
from app.services.wiki_curator import (
    CuratorChunk,
    CuratorResult,
    WikiCurator,
    deterministic_dedupe_key,
)
from app.services.wiki_curator_client import CuratorClient
from app.services.wiki_store import (
    WikiStore,
    normalize_slug,
    promotion_identity_key,
)

# Approximate bytes per text chunk for deterministic chunk-uid generation.
# Matches the 2 000-character window used by SemanticChunker.
_COMPILE_CHUNK_SIZE = 2000


def _suppressed_compile_result(reason: str) -> dict:
    """Shape-complete result for a suppressed compile (issue #1162).

    Every key a caller reads on the materializing path is present so the
    o04c supersede-count pins and the WIKI-011 document-status route keep
    working unchanged; `skipped` + `reason` feed the route's "skipped"
    vocabulary.
    """
    return {
        "page": None,
        "claims": [],
        "entities": [],
        "relations": [],
        "relations_count": 0,
        "claims_superseded": 0,
        "claims_weak_provenance": 0,
        "skipped": True,
        "reason": reason,
    }


# Extensions whose content the wiki compile fallback will even attempt to
# parse (final-critic round 1: the decision lives HERE, not in the shared
# DocumentParser). Covers the text formats the .txt fallback reaches AND
# the binary document formats unstructured.auto handles (PDF/DOCX/PPTX
# are ZIP/binary containers that always carry NULs — refusing them by
# sniff alone would suppress SUPPORTED types). Anything else combined
# with a binary head is a typed content-type refusal; a parse crash on
# one of THESE stays a failure.
_WIKI_PARSEABLE_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".text", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".odt", ".odp", ".ods", ".epub", ".eml", ".msg", ".rtf",
})


def _wiki_refuses_content_type(file_path: str) -> bool:
    """True when the wiki compile fallback refuses the file's content type.

    An extension NOT in the wiki-parseable allowlist whose first kilobyte
    contains a NUL byte (the Git binary heuristic — ZIP containers like
    DOCX/PPTX and most raw binaries always carry NULs) is refused BEFORE
    the parser runs. Text formats never emit NULs in their leading bytes,
    and the allowlisted extensions go to the parser regardless, so a
    parser crash on a SUPPORTED type remains an extractor failure.
    """
    suffix = Path(file_path).suffix.lower()
    if suffix in _WIKI_PARSEABLE_SUFFIXES:
        return False
    try:
        with open(file_path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return b"\x00" in head

# Matches [S#], [M#], [W#] citation markers produced by the chat engine.
_CITE_STRIP_RE = re.compile(r"\[(?:S|M|W)\d+\]")

logger = logging.getLogger(__name__)


def _numeric_multiset(text: str) -> tuple:
    """Sorted tuple of numeric tokens in ``text`` (F-03, PR #1113 review
    round 2): ``normalize_claim_text`` strips punctuation, so "5.10 MB"
    and "510 MB" normalize identically and a quote verified for one can
    promote the other. Requiring equal numeric multisets blocks those
    collisions while keeping punctuation-only variants matching."""
    return tuple(sorted(re.findall(r"\d+(?:\.\d+)?", text or "")))

# Module-level executor used when _maybe_run_curator is called from
# inside a running event loop. The previous per-call
# ``with ThreadPoolExecutor(max_workers=1) as ex: ...`` pattern paid
# the executor-construction cost on every curator invocation
# (issue #109 — DD-C010). A single shared executor avoids that and
# keeps the worker thread warm across calls. Curator calls are
# infrequent and never overlap heavily, so max_workers=1 is fine.
_CURATOR_OFFLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="wiki-curator"
)

# Cooperative shutdown flag (#799/#854 lifecycle drain): set by the lifespan
# wiki stage at shutdown; checked BETWEEN curator propose calls so an
# in-flight curate() orchestration aborts at the next call boundary instead
# of holding its worker for the full configured request sequence. Reset at
# lifespan startup for same-process restarts (embedded/TestClient lifespans).
CURATOR_SHUTDOWN = threading.Event()


def _get_curator_offload_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Recreate-on-demand accessor for the curator offload executor.

    The lifespan wiki stage shuts the executor down (``wait=False,
    cancel_futures=True``) so queued orchestration never extends interpreter
    exit; mirroring the wiki-lint accessor convention
    (``app.api.routes.wiki._get_wiki_lint_executor``, PR #1113 N-05a), this
    accessor detects the shutdown and hands out a FRESH executor so the next
    lifespan entry in the same interpreter (tests, embedded runs) can still
    schedule work.
    """
    global _CURATOR_OFFLOAD_EXECUTOR
    if getattr(_CURATOR_OFFLOAD_EXECUTOR, "_shutdown", False):
        _CURATOR_OFFLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wiki-curator"
        )
    return _CURATOR_OFFLOAD_EXECUTOR

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

    def _vault_row_exists(self, vault_id: Optional[int]) -> bool:
        """True when vault_id names a live vault row (None is never checked).

        Issue #723 (T1-21-S-07): compile entry points refuse to write wiki
        rows for a vault that was deleted while the job sat pending or was
        already running — the entry-point re-check narrows the write race to
        inside a single compile (an irreducible residual disclosed in the
        issue's PR).
        """
        if vault_id is None:
            return True
        row = self._db.execute(
            "SELECT 1 FROM vaults WHERE id = ?", (vault_id,)
        ).fetchone()
        return row is not None

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
        exact = self._store.find_claim_by_text(vault_id, claim_text)
        existing = exact or self._store.find_claim_by_normalized_text(
            vault_id, claim_text
        )
        # Issue #1117: the fuzzy (normalized) arm strips ALL punctuation, so
        # it can match a DIFFERENT statement ("U.S. economy shrank" vs "US
        # economy shrank"). Dedup/source-attach may still ride the fuzzy
        # match, but the STATUS LIFTS below additionally require text
        # identity: an exact-arm match, or equality under
        # promotion_identity_key (which tolerates trailing `!`/`?` only —
        # a trailing PERIOD is identity-relevant and blocks the lift,
        # while case and whitespace-run differences ARE tolerated — the
        # "Alpha is verified" ≡ "Alpha is verified!" arm the #929
        # parity pins).
        liftable = exact is not None or (
            existing is not None
            and promotion_identity_key(existing.claim_text)
            == promotion_identity_key(claim_text)
        )
        if existing:
            # Promote unverified → active when the caller has explicit per-claim citations.
            new_status = create_kwargs.get("status")
            if (
                existing.status == "unverified"
                and new_status == "active"
                and liftable
            ):
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
            # WIKI-006 (#515): reactivate superseded → active when this compile
            # re-derives the claim text unchanged from a current source (e.g. a
            # reindex re-promoting a memory whose claim sentence survived an
            # edit). Only the found-by-text claim is touched, and only from
            # 'superseded' — a claim whose supporting content changed resolves
            # to a DIFFERENT sentence, so it never reaches this branch; on the
            # file-recompile path such claims are retired by the post-pass in
            # compile_ingest_job (issue #794), and on the memory-edit path by
            # mark_claims_stale_by_memory.
            if (
                existing.status == "superseded"
                and new_status == "active"
                and liftable
            ):
                if self._store.reactivate_claim(existing.id):
                    existing.status = "active"
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
        is_admin: bool = False,
    ) -> dict:
        """
        Promote a memory record into wiki pages, entities, claims, and relations.

        Returns dict with keys: page, claims, entities, relations.

        ``is_admin`` gates promotion of GLOBAL memories (``vault_id IS NULL``).
        Global memories are admin-only under the #404 security model, so a
        non-admin caller — who legitimately has write access to a vault — must
        not be able to promote (and thereby read the content of) a global
        memory into their vault. Defaults to ``False`` (fail-closed) so a
        caller that forgets the flag cannot promote a global memory. The
        interactive ``/wiki/promote-memory`` route passes the caller's
        role-derived value; trusted system callers (e.g. the background
        WikiCompileProcessor) pass ``True`` explicitly.
        """
        if not self._vault_row_exists(vault_id):
            return {"skipped": True, "reason": f"vault {vault_id} no longer exists"}

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
        # Global-memory tier (vault_id IS NULL) is admin-only (issue #404).
        # Without this, any user with write access to a vault could promote a
        # global memory and read its content. The existing
        # ``memory_vault_id != vault_id`` check above is skipped for global
        # memories (NULL is never equal to an int), so this explicit gate is
        # required.
        #
        # Raise ValueError (→ 404 at the route) rather than PermissionError
        # (→ 403) so a non-admin prober cannot distinguish "no such memory"
        # from "global memory exists" via the status code (PRR-008
        # existence-oracle). Admins never reach this branch (is_admin=True).
        if memory_vault_id is None and not is_admin:
            raise ValueError(f"Memory {memory_id} not found")

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
        if not self._vault_row_exists(vault_id):
            return {"skipped": True, "reason": f"vault {vault_id} no longer exists"}

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
                    # WIKI-008 (#515): reload BY CLAIM ID, not by exact input
                    # text — the reuse may have matched via normalized text
                    # (punctuation/whitespace variant), and an exact-text
                    # lookup then misses the claim and returns an empty
                    # source snapshot, which re-attaches duplicate rows.
                    _current_sources = list(
                        self._store.get_claim_sources(claim.id)
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

            # Lint finding for every unverified claim (only on first creation).
            # origin='compiler' (issue #791): the lint reconciler must never
            # auto-resolve these rows — they belong to the compile path.
            if _created and claim_status == "unverified":
                self._db.execute(
                    """INSERT INTO wiki_lint_findings
                       (vault_id, finding_type, severity, title, details,
                        related_page_ids_json, related_claim_ids_json, status, origin, created_at, updated_at)
                       VALUES (?, 'unsupported_claim', 'low', ?, ?, '[]', ?, 'open', 'compiler', ?, ?)""",
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
        if not self._vault_row_exists(vault_id):
            return {
                "page": None,
                "claims": [],
                "entities": [],
                "relations_count": 0,
                "skipped": True,
                "reason": f"vault {vault_id} no longer exists",
            }

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
        # Issue #1162 taxonomy: an explicitly empty/whitespace text is a
        # successful-empty document — skip BEFORE any parse attempt (no
        # provider calls, no claim mutation), preserving the o04b no-wipe
        # contract.
        explicit_text = input_json.get("text")
        if explicit_text is not None and not str(explicit_text).strip():
            return _suppressed_compile_result("empty text")
        text: str = explicit_text or file_data.get("parsed_text") or ""

        if not text:
            _file_path = file_data.get("file_path") or ""
            _parse_failure: Optional[Exception] = None
            if _file_path:
                from app.services.document_processor import (
                    DocumentParseError as _DPE,
                )
                from app.services.document_processor import (
                    DocumentParser,
                    UnsupportedContentTypeError,
                )
                # Issue #1162 typed refusal (final-critic rounds 1-2): decide
                # unsupported content types HERE, in the wiki compile
                # fallback only, by an explicit extension allowlist plus a
                # binary sniff — NEVER inside the shared DocumentParser,
                # whose supported set includes NUL-leading binary formats
                # (PDF/DOCX/PPTX) that main ingestion must keep parsing.
                # The typed error is raised and caught right here so the
                # signal stays real (one raise site, one handler).
                try:
                    if _wiki_refuses_content_type(_file_path):
                        raise UnsupportedContentTypeError(
                            f"Binary or unsupported content type for wiki "
                            f"compile (path_len={len(_file_path)})"
                        )
                except UnsupportedContentTypeError:
                    return _suppressed_compile_result("unparseable content type")
                # Attempt 1: full parser stack (requires unstructured.io).
                try:
                    _elements = DocumentParser().parse(_file_path)
                    text = "\n".join(str(e) for e in _elements if str(e).strip())
                except _DPE as _exc:
                    logger.debug(
                        "compile_ingest_job: DocumentParser failed for file_id=%d: %s",
                        file_id, _exc,
                    )
                    _parse_failure = _exc
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
                if not text and _parse_failure is not None:
                    # Extractor failure on an accepted type with an unusable
                    # fallback remains a failure (owner decision, Nov 18 2026).
                    raise _parse_failure
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
            return _suppressed_compile_result("no text available")
        if not str(text).strip():
            # Whitespace-only content after acquisition: a successful-empty
            # document (same taxonomy as explicit empty text).
            return _suppressed_compile_result("empty text")

        # Issue #1162 (owner decision, November 18 2026): classify the
        # document's blocks once; only CONTENT blocks can qualify it. TOC /
        # boilerplate / code never yield qualifying items, procedures, or
        # claims.
        prose = content_text(text)
        extraction = extract_entities_from_text(prose)
        procedures = extract_procedures(text)
        # AC9 gate (#1162): subjectless deterministic candidates are rejected
        # BEFORE qualification — they can neither create claims/relations nor
        # materialize records. The filtered list feeds the persistence loop,
        # the curator dedupe seed, and the deterministic summary.
        valid_role_claims = [
            rc for rc in extraction.role_claims
            if (rc.get("subject") or "").strip()
        ]
        claims_rejected_subjectless = len(extraction.role_claims) - len(valid_role_claims)
        extraction.role_claims = valid_role_claims

        # Curator ANALYSIS with deferred persistence (issue #1162): the run
        # happens before the materialization decision so substantive curator
        # output can qualify the document; accepted claims persist exactly
        # once, after the page exists. The patch seam (`_maybe_run_curator`)
        # keeps its name and default legacy contract for the other callers.
        cur_out = self._maybe_run_curator(
            vault_id=vault_id,
            file_id=file_id,
            text=text,
            trigger="ingest",
            deterministic_extraction=extraction,
            page_id=None,
            existing_entities=[],
            defer_persist=True,
        )
        qualifying = (
            bool(extraction.acronyms)
            or bool(valid_role_claims)
            or bool(procedures)
            or self._curator_accepted_count(cur_out) > 0
        )

        if not qualifying:
            # Suppression gates CREATION only (issue #1162): a first-seen
            # unqualifying document creates nothing; existing derived records
            # keep their non-destructive recompile lifecycle.
            if cur_out is not None:
                self._persist_curator_result(
                    cur_out, vault_id=vault_id, page_id=None, lint_only=True
                )
            # Owner decision 2026-11-25 (ASK-001, critic's option c): a failed
            # enabled-curator qualification stays suppressed, but is never
            # reported as a successful empty extraction. The distinct reason
            # keeps the AC1 distinction between success and failure intact.
            reason = (
                "curator unavailable"
                if self._curator_failed(cur_out)
                else "no qualifying items"
            )
            result = _suppressed_compile_result(reason)
            if claims_rejected_subjectless:
                result["claims_rejected_subjectless"] = claims_rejected_subjectless
            existing_pages = self._store.list_pages_by_file(
                vault_id=vault_id, file_id=file_id
            )
            if existing_pages:
                # Refresh every file-associated page (entity AND procedure
                # pages) and retire claims whose quotes vanished in this
                # revision; nothing is deleted (cleanup stays planning-only).
                supersede_counts = self._store.supersede_unrederived_file_claims(
                    file_id=file_id,
                    vault_id=vault_id,
                    page_id=existing_pages[0].id,
                    keep_claim_ids=set(),
                    current_text=text,
                )
                self._db.commit()
                for page in existing_pages:
                    self._store.update_page(
                        page_id=page.id,
                        vault_id=vault_id,
                        markdown=text[:2000],
                        summary="",
                        last_compiled_at=datetime.utcnow().isoformat(),
                        status="stale",
                    )
                result["claims_superseded"] = supersede_counts["superseded"]
                result["claims_weak_provenance"] = supersede_counts["weak_provenance"]
            if cur_out is not None:
                result["curator"] = self._curator_summary(cur_out)
            return result

        file_name = file_data.get("file_name") or f"file:{file_id}"
        title = file_name

        # WIKI-009 (#515): a document's page is resolved through the
        # wiki_page_files association (stable per file), NOT through a slug
        # derived only from the file_name — two different files sharing a
        # file_name in one vault would otherwise collide onto a single page
        # and clobber each other's compiler-owned content.
        page = self._store.get_page_by_file(vault_id=vault_id, file_id=file_id)
        created = False
        if page is None:
            slug = normalize_slug(f"document/{file_name[:60]}")
            existing = self._db.execute(
                "SELECT id FROM wiki_pages WHERE vault_id = ? AND slug = ?", (vault_id, slug)
            ).fetchone()
            if existing is not None and self._db.execute(
                "SELECT 1 FROM wiki_page_files WHERE vault_id = ? AND page_id = ? "
                "AND file_id != ? LIMIT 1",
                (vault_id, existing[0], file_id),
            ).fetchone():
                # Another file already owns that slug in this vault — append
                # the stable file id so each same-named document gets (and,
                # on recompile, keeps) its own page.
                slug = normalize_slug(f"document/{file_name[:52]}-{file_id}")
                existing = self._db.execute(
                    "SELECT id FROM wiki_pages WHERE vault_id = ? AND slug = ?",
                    (vault_id, slug),
                ).fetchone()
            if existing is not None:
                # No other file owns the slug: adopt the page (this also
                # picks up pages created before file associations existed)
                # and record this file's ownership.
                page = self._store.get_page(existing[0], load_relations=False)
                self._store.attach_file(page.id, file_id, vault_id)  # type: ignore[union-attr]
            else:
                page = self._store.create_page(
                    vault_id=vault_id,
                    title=title,
                    page_type="entity",
                    slug=slug,
                    markdown=text[:2000],
                    status="needs_review",
                )
                self._store.attach_file(page.id, file_id, vault_id)
                created = True
        if not created:
            # WIKI-007 (#515): refresh compiler-owned page content on
            # recompile so the wiki reflects the CURRENT source text.
            # POLICY LIMIT: wiki_pages has no manually_edited flag, so the
            # compiler cannot distinguish manual edits from its own output;
            # the refresh is unconditional, and update_page snapshots the
            # prior content into wiki_page_versions first so history always
            # preserves it.
            self._store.update_page(
                page_id=page.id,  # type: ignore[union-attr]
                vault_id=vault_id,
                markdown=text[:2000],
                summary="",
                last_compiled_at=datetime.utcnow().isoformat(),
            )

        slug = page.slug  # type: ignore[union-attr]
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

        # Issue #794 feedback: surface what the supersede post-pass retired so
        # the compile job's result_json (persisted with the job and returned
        # by the document-status route) shows the
        # retirement instead of leaving it observable only via lint rows.
        supersede_counts = self._store.supersede_unrederived_file_claims(
            file_id=file_id,
            vault_id=vault_id,
            page_id=page_id,
            keep_claim_ids={c["id"] for c in claims_created},
            current_text=text,
        )
        self._db.commit()

        # Procedure pages (issue #1162 AC3): every detected procedure gets a
        # page whose steps keep their numbers and order, attached to the
        # source file so the recompute lifecycle covers them.
        self._upsert_procedure_pages(
            vault_id=vault_id, file_id=file_id, file_name=file_name,
            procedures=procedures,
        )

        # Curator PERSISTENCE (deferred from the pre-materialization
        # analysis): accepted claims + sources land exactly once, with the
        # real page id; lint findings persist on both branches (the
        # suppressed branch called this helper with lint_only=True).
        # Errors were already recorded in the analysis summary, never raised.
        curator_summary = None
        if cur_out is not None:
            curator_summary = self._persist_curator_result(
                cur_out, vault_id=vault_id, page_id=page_id
            )

        # Gated KMS document entry (issue #1162): the wiki ingest worker is
        # the single qualification owner, so a qualifying document always
        # carries BOTH derived records regardless of queue order — but the
        # independent KMS subsystem flags stay authoritative: when KMS is
        # disabled or not compiled on ingest, this wiki-side write is a
        # no-op and the KMS job (its own gate) owns the entry.
        self._upsert_kms_document_entry(
            vault_id=vault_id, file_id=file_id, file_name=file_name, body=text
        )

        page = self._store.get_page(page_id)
        result_dict: dict = {
            "page": {"id": page_id, "slug": slug} if page else None,
            "claims": claims_created,
            "entities": [{"id": e.id, "name": e.canonical_name} for e in entities_created],
            "relations_count": relations_count,
            "claims_superseded": supersede_counts["superseded"],
            "claims_weak_provenance": supersede_counts["weak_provenance"],
        }
        if claims_rejected_subjectless:
            result_dict["claims_rejected_subjectless"] = claims_rejected_subjectless
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
        defer_persist: bool = False,
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
            # Issue #795: ALWAYS dispatch through the module-level
            # single-worker offload executor (issue #109). The previous
            # inline asyncio.run branch ran whenever no loop was active in
            # the calling thread — exactly the case for both real callers,
            # which reach the compiler via asyncio.to_thread pool threads
            # (the background processor and the promote-memory route) — so
            # two concurrent jobs could drive two concurrent event loops
            # against one shared curator breaker. The single worker
            # serializes every curator loop process-wide; within a job the
            # concurrency knob still bounds parallel propose calls. The
            # wait on the result is intentionally untimed: the per-call
            # HTTP timeout bounds each request, and the curator must never
            # fail the enclosing job.
            coro = curator.curate(
                vault_id=vault_id,
                file_id=file_id,
                chunks=chunks,
                deterministic_summary=deterministic_summary,
                existing_entities_brief=existing_brief,
                deterministic_dedupe_keys=dedupe_seed,
            )
            # Issue #795 review: submit does NOT copy contextvars, so a bare
            # submit(asyncio.run, coro) would drop the caller's request
            # identity (traceparent/X-Request-ID) from every curator POST
            # and log line. Copy the context here and run the coroutine
            # inside it on the worker thread.
            submit_ctx = contextvars.copy_context()
            cur_result = _get_curator_offload_executor().submit(
                submit_ctx.run, asyncio.run, coro
            ).result()
        except Exception as e:  # broad — curator must never fail the job
            logger.warning(
                "wiki curator: orchestration failed for vault=%s file=%s: %s",
                vault_id, file_id, e,
            )
            # Issue #958: these strings reach vault readers through the
            # job's result_json — exception CLASS only (the adjacent lint
            # path's discipline); the message stays in the log line above.
            return {
                "errors": [f"orchestration_error: {type(e).__name__}"],
                "accepted": 0,
                "rejected": 0,
                "lint": 0,
                "calls": 0,
                "input_chars": 0,
            }

        if defer_persist:
            # Analysis-only handoff (issue #1162): the ingest path decides
            # materialization first and persists via _persist_curator_result
            # once the page exists (or lint-only when suppressed).
            return cur_result
        return self._persist_curator_result(cur_result, vault_id=vault_id, page_id=page_id)

    def _persist_curator_result(
        self,
        cur_result,
        *,
        vault_id: int,
        page_id: Optional[int],
        lint_only: bool = False,
    ) -> Optional[dict]:
        """Persist curator output (accepted claims + sources, lint findings).

        Issue #1162 split: analysis lives in ``_maybe_run_curator``; this
        helper owns persistence. The DEFAULT mode is the exact legacy
        contract — accepted claims and sources persist WITH the caller's
        page_id (including a legitimately-None query/direct page_id), and
        attach_source failures surface as persist errors in the summary.
        ``lint_only=True`` (suppressed ingest only) persists findings and
        nothing else — a nullable page_id alone never disables claim
        persistence. A non-CuratorResult value (dict double or legacy
        summary) passes through untouched: it carries no claim objects.
        """
        if not isinstance(cur_result, CuratorResult):
            return cur_result
        if lint_only:
            cur_result.accepted = []

        # Persist accepted candidates as wiki_claims with provenance.
        # WIKI-010 (#515): the claim row AND its source row commit as one
        # explicit BEGIN IMMEDIATE ... COMMIT transaction. Previously only
        # create_claim committed — attach_source's insert stayed pending on
        # the connection and only reached the DB if a LATER write (e.g. lint
        # findings) happened to commit, so an accepted claim could be visible
        # without its provenance, or lose it entirely on return-to-pool.
        # PRR-001 (#531): each claim+source pair is additionally wrapped in a
        # SAVEPOINT, so the pair is atomic even when it fails — the per-claim
        # except below rolls the pair back (a failed attach_source can no
        # longer leave an orphaned committed claim without its source) while
        # the batch continues with the next candidate. The SAVEPOINT/RELEASE
        # statements run on self._db, the SAME connection WikiStore wraps (its
        # transaction() opened the surrounding BEGIN IMMEDIATE on it).
        with self._store.transaction():
            for accepted in cur_result.accepted:
                self._db.execute("SAVEPOINT claim_pair")
                try:
                    # Find-or-create (issue #791 / T1-35-S-03): a repeated
                    # claim_text used to hit UNIQUE(vault_id, claim_text),
                    # roll the claim+source savepoint back, and silently drop
                    # this file's provenance. Replicate _find_or_create_claim's
                    # lookup + source-dedupe semantics INLINE — its own
                    # promotion/reactivation branches commit internally, which
                    # would break the pair-atomicity established above.
                    # Issue #1117: same identity discipline as the
                    # deterministic twin — the fuzzy arm may match a
                    # punctuation-differing statement, so record which arm
                    # matched and require text identity for the lifts.
                    _exact = self._store.find_claim_by_text(
                        vault_id, accepted.claim_text
                    )
                    existing = _exact or self._store.find_claim_by_normalized_text(
                        vault_id, accepted.claim_text
                    )
                    _liftable = _exact is not None or (
                        existing is not None
                        and promotion_identity_key(existing.claim_text)
                        == promotion_identity_key(accepted.claim_text)
                    )
                    if existing is not None:
                        claim = existing
                        # Issue #794 feedback: the supersede post-pass runs
                        # BEFORE the curator, so a claim it just superseded
                        # (or one superseded by an earlier edit) can match
                        # here by text while its quote is verifiably present
                        # again. Reactivate it with the same no-commit
                        # helper the deterministic path uses — leaving it
                        # superseded would bury live, quote-verified
                        # knowledge (the curator's find-or-create
                        # deliberately omits reactivation to keep
                        # pair-atomicity, so the flip must be inline too).
                        # Issue #795 review: gate on THIS candidate's own
                        # computed status — the deterministic twin gates
                        # identically (new_status == "active") — so a
                        # needs_review candidate (draft mode, unknown
                        # chunk, quote check off) can never resurrect a
                        # superseded claim as active. Issue #1117: the lift
                        # also requires text identity (exact arm or
                        # promotion_identity_key equality) — a
                        # punctuation variant of a superseded claim is NOT
                        # the same statement and must not resurrect it.
                        if (
                            existing.status == "superseded"
                            and accepted.status == "active"
                            and _liftable
                        ):
                            self._store.reactivate_claim_if_superseded(existing.id)
                            existing.status = "active"
                        # Issue #929: promote unverified → active with the
                        # same commit-free discipline (the deterministic
                        # twin _find_or_create_claim lifts status +
                        # source_type + confidence and then commits; inside
                        # this savepoint the commit belongs to the pair, so
                        # replicate the lift with a predicated UPDATE — the
                        # status predicate makes the check-and-flip atomic
                        # the way reactivate_claim_if_superseded's is).
                        # source_type lifts to 'document' only when the
                        # curator's evidence IS document-backed
                        # (accepted.file_id set — the ingest trigger); on
                        # the manual/query triggers the curator has no
                        # document, so a memory-sourced claim keeps its
                        # own source_type instead of being relabeled
                        # (implementation-review round 1).
                        if (
                            existing.status == "unverified"
                            and accepted.status == "active"
                            # F-03: the normalized lookup strips
                            # punctuation, so require identical numeric
                            # tokens before lifting (blocks 5.10-vs-510
                            # promotions on the lossy-match path).
                            and _numeric_multiset(existing.claim_text)
                            == _numeric_multiset(accepted.claim_text)
                            # Issue #1117: the numeric gate passes for
                            # non-numeric-differing texts, so ALSO require
                            # text identity (exact arm or
                            # promotion_identity_key equality) before the
                            # lift.
                            and _liftable
                        ):
                            cur = self._db.execute(
                                "UPDATE wiki_claims SET status = 'active',"
                                " source_type = ?, confidence = ?,"
                                " updated_at = datetime('now')"
                                " WHERE id = ? AND status = 'unverified'",
                                (
                                    "document" if accepted.file_id
                                    else existing.source_type,
                                    accepted.confidence,
                                    existing.id,
                                ),
                            )
                            if cur.rowcount:
                                existing.status = "active"
                        already = any(
                            s.source_kind == "document"
                            and s.file_id == accepted.file_id
                            for s in existing.sources
                        )
                    else:
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
                            commit=False,
                        )
                        already = False
                    if not already:
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
                    self._db.execute("RELEASE claim_pair")
                except Exception as e:  # pragma: no cover - defensive
                    # Undo this pair only; RELEASE the (still-open) savepoint
                    # so the next iteration starts from a clean savepoint
                    # stack inside the still-open outer transaction.
                    self._db.execute("ROLLBACK TO claim_pair")
                    self._db.execute("RELEASE claim_pair")
                    cur_result.errors.append(
                        f"persist_error: {type(e).__name__}"
                    )
                    logger.warning(
                        "wiki curator: failed to persist accepted claim: %s", e
                    )

        # Persist lint findings. One fingerprint-aware upsert carries the
        # whole list with origin='curator' (issue #791 / T1-35-S-03): a
        # curator rerun used to insert a fresh open copy of each finding via
        # the raw create_lint_finding INSERT. Curator rows are also outside
        # the linter's origin-scoped resolve pass, so a lint run can never
        # force-resolve them. The try/except keeps the per-finding loop's
        # recorded-never-raised contract (the orchestration catch above
        # returns early and does not cover this block): a persist failure is
        # recorded into the summary, never raised into the compile job.
        if cur_result.lint_findings:
            # Issue #791 review: the single upsert is one transaction, so a
            # malformed spec would abort the whole batch where the old
            # per-finding loop dropped only the offender. Validate each spec
            # first and quarantine the invalid ones into errors, restoring
            # that isolation without giving up the batch dedupe.
            specs = []
            for finding in cur_result.lint_findings:
                problem = self._curator_spec_problem(finding)
                if problem is not None:
                    cur_result.errors.append(f"lint_spec_error: {problem}")
                    continue
                specs.append(
                    {
                        "finding_type": finding["finding_type"],
                        "severity": finding.get("severity", "medium"),
                        "title": finding.get("title", "Curator lint"),
                        "details": finding.get("details", ""),
                        "related_page_ids": finding.get("related_page_ids", []),
                        "related_claim_ids": finding.get("related_claim_ids", []),
                    }
                )
            if specs:
                try:
                    self._store.upsert_lint_findings(vault_id, specs, origin="curator")
                except Exception as e:  # pragma: no cover - defensive
                    # Exception CLASS only (issue #791 review): sqlite3 driver
                    # messages quote CHECK expressions and table/column names,
                    # and this string reaches vault readers through the job's
                    # result_json.
                    cur_result.errors.append(
                        f"lint_persist_error: {type(e).__name__}"
                    )

        return cur_result.to_summary()

    @staticmethod
    def _curator_accepted_count(cur_out) -> int:
        """Accepted-curator count from a CuratorResult OR a legacy dict double.

        Issue #1162 compatibility adapter: the deferred-analysis seam may
        hand back either the real CuratorResult or a summary-shaped dict
        (the frozen C4 double replaces the whole seam method).
        """
        if cur_out is None:
            return 0
        if isinstance(cur_out, dict):
            try:
                return int(cur_out.get("accepted") or 0)
            except (TypeError, ValueError):
                return 0
        return len(getattr(cur_out, "accepted", None) or [])

    @staticmethod
    def _curator_failed(cur_out) -> bool:
        """Whether the enabled curator reported an error (owner decision 2026-11-25).

        A failed qualification must never be reported as a successful empty
        extraction. The ingest path uses this to give suppression the distinct
        ``curator unavailable`` reason while still creating nothing.
        """
        if cur_out is None:
            return False
        errors = (
            cur_out.get("errors")
            if isinstance(cur_out, dict)
            else getattr(cur_out, "errors", None)
        )
        return bool(errors)

    @staticmethod
    def _curator_summary(cur_out):
        """JSON-safe summary for ``result_json.curator`` from either shape."""
        if cur_out is None:
            return None
        if isinstance(cur_out, dict):
            return cur_out
        return cur_out.to_summary()

    def _upsert_procedure_pages(
        self,
        *,
        vault_id: int,
        file_id: int,
        file_name: str,
        procedures: List[Procedure],
    ) -> None:
        """Create or refresh one procedure page per detected procedure.

        Issue #1162 AC3: steps keep their numbers and order verbatim; the
        markdown follows the frontend template shape (WikiEditDialog). Pages
        are attached to the source file so the unqualifying-recompute
        lifecycle (list_pages_by_file) covers them.
        """
        for index, procedure in enumerate(procedures, start=1):
            slug = normalize_slug(f"procedure/{file_name[:40]}-{file_id}-{index}")
            markdown = (
                f"# {procedure.title}\n\n## Purpose\n\n\n\n## Steps\n\n"
                + "\n".join(procedure.steps)
                + "\n\n## Notes\n\n"
            )
            existing = self._db.execute(
                "SELECT id FROM wiki_pages WHERE vault_id = ? AND slug = ?",
                (vault_id, slug),
            ).fetchone()
            if existing is not None:
                self._store.update_page(
                    page_id=existing[0],
                    vault_id=vault_id,
                    markdown=markdown,
                    last_compiled_at=datetime.utcnow().isoformat(),
                )
                continue
            page = self._store.create_page(
                vault_id=vault_id,
                title=procedure.title[:200] or slug,
                page_type="procedure",
                slug=slug,
                markdown=markdown,
                status="needs_review",
            )
            self._store.attach_file(page.id, file_id, vault_id)

    def _upsert_kms_document_entry(
        self,
        *,
        vault_id: int,
        file_id: int,
        file_name: str,
        body: str,
    ) -> None:
        """Wiki-owned KMS document entry (issue #1162), gated on KMS flags.

        The wiki ingest worker is the single qualification owner, so a
        qualifying document carries BOTH derived records regardless of the
        two queues' ordering — but the independent KMS subsystem flags stay
        authoritative (kms_enabled AND kms_compile_on_ingest, the same pair
        that gates the KMS enqueue); when KMS is off this is a no-op. The
        upsert is idempotent per (vault, file, source_type=document), so it
        converges with the KMS job's own write instead of duplicating.
        """
        if not (
            getattr(_app_settings, "kms_enabled", False)
            and getattr(_app_settings, "kms_compile_on_ingest", False)
        ):
            return
        from app.services.kms_store import KMSStore

        KMSStore(self._db).upsert_document_entry(
            vault_id=vault_id,
            file_id=file_id,
            title=file_name or f"Document {file_id}",
            body=body,
            summary=entry_summary(body),
        )

    # Vocabularies enforced by the wiki_lint_findings CHECK constraints
    # (backend/app/models/database.py). A curator spec outside them would
    # abort the whole batched upsert.
    _LINT_FINDING_TYPES = frozenset(
        {
            "contradiction",
            "stale",
            "orphan",
            "missing_page",
            "unsupported_claim",
            "duplicate_entity",
            "weak_provenance",
        }
    )
    _LINT_SEVERITIES = frozenset({"low", "medium", "high", "critical"})

    @classmethod
    def _curator_spec_problem(cls, finding: Any) -> Optional[str]:
        """Return None when a curator lint spec is persistable, else a short
        reason string for the run's error list (issue #791 review)."""
        if not isinstance(finding, dict):
            return "spec_not_a_dict"
        if "finding_type" not in finding:
            return "missing_finding_type"
        if finding["finding_type"] not in cls._LINT_FINDING_TYPES:
            return "unknown_finding_type"
        severity = finding.get("severity", "medium")
        if severity not in cls._LINT_SEVERITIES:
            return "unknown_severity"
        for key in ("related_page_ids", "related_claim_ids"):
            value = finding.get(key, [])
            if not isinstance(value, list):
                return f"{key}_not_a_list"
        return None

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
        # Take from the start of the text up to budget. This is the
        # per-document curated span (never below one 2000-char chunk) — the knob's documented semantics
        # (issue #795) — NOT a per-call prompt bound; build_user_prompt
        # re-applies its own bound to each call's chunk snippet, so we
        # don't need to paginate here.
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
