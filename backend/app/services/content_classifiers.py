"""Shared content-block classification for the wiki/KMS compile pipeline.

Issue #1162 (Workstream L PR 6, owner decision November 18 2026): one
classifier governs (a) whether a document's derived records qualify for
creation under the approved suppression policy, (b) what a KMS entry's
summary contains, (c) which chunks are retrievable, and (d) which numbered
runs become procedure pages. Keeping these in one module prevents the
composition holes per-consumer detectors would open: a numbered
table-of-contents must never qualify a document as containing a procedure,
and an acronym inside a licence block is not extractable knowledge.

Block kinds:
- ``content``     substantive prose (the only source of qualifying items)
- ``toc``         table-of-contents blocks (dotted-leader entries or a
                  Contents heading followed by entry lines)
- ``boilerplate`` licence / copyright / change-record blocks
- ``code``        fenced code spans (```-delimited), never procedures
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

__all__ = [
    "Block",
    "Procedure",
    "classify_blocks",
    "content_text",
    "entry_summary",
    "is_toc_text",
    "is_boilerplate_text",
    "extract_procedures",
    "document_has_qualifying_items",
]

# KMS entry summaries cap at this many characters of content-block text
# (matches the historic leading-characters limit in kms_compile_processor).
ENTRY_SUMMARY_CHARS = 500


@dataclass(frozen=True)
class Block:
    kind: str  # 'content' | 'toc' | 'boilerplate' | 'code'
    text: str


_FENCE_OPEN_RE = re.compile(r"^\s*(```|~~~)")
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)[.)]\s+\S")
_TOC_LEADER_RE = re.compile(
    # "Entry text ........ 12" / "Entry .... 3-4" / "Entry …… iv"
    r"\s*[.\u2026]{2,}\s*(?:\d+(?:\s*[-\u2013]\s*\d+)?|[ivxlcdm]+)\s*$",
    re.IGNORECASE,
)
_CONTENTS_HEADING_RE = re.compile(r"^\s*(table of contents|contents)\s*:?\s*$", re.IGNORECASE)
_BOILERPLATE_RE = re.compile(
    r"(licence|license|copyright|\u00a9|all rights reserved|"
    r"revision history|changelog|change record)",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")

# A block is a TOC when at least this fraction of its non-empty lines are
# dotted-leader entries, or it carries a Contents heading with >= 2 entries.
_TOC_LINE_FRACTION = 0.6
_TOC_MIN_ENTRIES = 2
# A procedure is a run of at least this many consecutive numbered lines.
_PROCEDURE_MIN_STEPS = 2


def _split_blocks(text: str) -> List[List[str]]:
    """Split into blank-line-separated blocks, keeping fenced spans intact."""
    blocks: List[List[str]] = []
    current: List[str] = []
    in_code = False
    for line in text.splitlines():
        if _FENCE_OPEN_RE.match(line):
            # Toggle on every fence marker line; a block that opens a fence
            # stays one block until the closing marker (fences may contain
            # blank lines).
            if current and not in_code:
                blocks.append(current)
                current = []
            in_code = not in_code
            current.append(line)
            continue
        if in_code:
            current.append(line)
            continue
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _classify_block(lines: List[str]) -> str:
    if lines and _FENCE_OPEN_RE.match(lines[0]):
        return "code"
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return "content"
    if _CONTENTS_HEADING_RE.match(non_empty[0]):
        rest = non_empty[1:]
        entries = sum(1 for ln in rest if _TOC_LEADER_RE.search(ln))
        # Under a Contents heading the block is TOC furniture only when the
        # rest of the block is entirely entry lines — a trailing prose line
        # (a chunk that merged TOC with following content) keeps the block
        # substantive. A heading alone (entries split off by a blank line)
        # is TOC too.
        if len(rest) == 0 or (entries >= _TOC_MIN_ENTRIES and entries == len(rest)):
            return "toc"
    leader_lines = sum(1 for ln in non_empty if _TOC_LEADER_RE.search(ln))
    if non_empty and leader_lines / len(non_empty) >= _TOC_LINE_FRACTION and leader_lines >= _TOC_MIN_ENTRIES:
        return "toc"
    if _BOILERPLATE_RE.search(non_empty[0]):
        return "boilerplate"
    return "content"


def classify_blocks(text: str) -> List[Block]:
    """Classify ``text`` into ordered blocks."""
    return [
        Block(kind=_classify_block(lines), text="\n".join(lines))
        for lines in _split_blocks(text or "")
    ]


def content_text(text: str) -> str:
    """The substantive prose of ``text`` (content blocks joined by blank lines)."""
    return "\n\n".join(b.text for b in classify_blocks(text) if b.kind == "content")


def is_toc_text(block_text: str) -> bool:
    """True when a standalone block (e.g. a chunk) classifies as a TOC."""
    lines = [ln for ln in (block_text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    return _classify_block(lines) == "toc"


def is_boilerplate_text(block_text: str) -> bool:
    """True when a standalone block classifies as boilerplate."""
    lines = [ln for ln in (block_text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    return _classify_block(lines) in ("boilerplate", "toc")


def entry_summary(body: str, limit: int = ENTRY_SUMMARY_CHARS) -> str:
    """KMS entry summary: leading content-block text, boilerplate excluded.

    Issue #1162 AC4: licence/copyright/change-record/TOC blocks are excluded
    from an entry's summary (the summary column is FTS-indexed, so
    boilerplate there is directly searchable noise). An all-boilerplate body
    yields an empty summary; the body itself stays complete.
    """
    prose = content_text(body or "")
    if not prose:
        return ""
    return prose[:limit].strip()


@dataclass(frozen=True)
class Procedure:
    title: str
    steps: List[str]


def extract_procedures(text: str) -> List[Procedure]:
    """Numbered-procedure runs inside CONTENT blocks (never TOC or code).

    Each run of >= 2 consecutive ``1.``-style lines becomes one procedure;
    the title is the nearest preceding heading line (or a generated title
    from the first step). Step lines are kept verbatim so numbers and order
    survive into the procedure page.
    """
    procedures: List[Procedure] = []
    for block in classify_blocks(text or ""):
        if block.kind != "content":
            continue
        current_title = ""
        run: List[str] = []
        for line in block.text.splitlines():
            if _HEADING_RE.match(line):
                current_title = line.lstrip("#").strip()
                run = []
                continue
            if _NUMBERED_LINE_RE.match(line):
                run.append(line)
                continue
            if len(run) >= _PROCEDURE_MIN_STEPS:
                procedures.append(_make_procedure(current_title, run))
            run = []
        if len(run) >= _PROCEDURE_MIN_STEPS:
            procedures.append(_make_procedure(current_title, run))
    return procedures


def _make_procedure(title: str, run: List[str]) -> Procedure:
    if not title:
        first = run[0].strip()
        title = first[:80]
    return Procedure(title=title, steps=list(run))


def document_has_qualifying_items(text: str) -> bool:
    """The approved document-level qualification rule (issue #1162).

    A document qualifies when its CONTENT blocks yield a deterministic
    acronym/role-claim extraction or a procedure. Pure function; safe to
    call from both compile workers (lazy import avoids the import cycle
    with wiki_compiler, which imports this module at load time).
    """
    from app.services.wiki_compiler import extract_entities_from_text

    prose = content_text(text or "")
    if not prose:
        return False
    extraction = extract_entities_from_text(prose)
    if extraction.acronyms:
        return True
    if any(
        (rc.get("subject") or "").strip()
        for rc in extraction.role_claims
    ):
        return True
    if extract_procedures(text or ""):
        return True
    return False
