"""
Lightweight helpers for mapping [S#]/[M#]/[W#] citation labels to source dicts.

Kept in a separate module with no heavy imports so it can be tested
independently of the FastAPI/auth import chain.
"""

import re
from typing import Any, Dict, List, Optional

_CITE_RE = re.compile(r"\[(S|M|W)(\d+)\]")
_CITE_STRIP = re.compile(r"\[(?:S|M|W)\d+\]")


def build_per_claim_sources(
    answer: str,
    doc_sources: List[Dict[str, Any]],
    memories_as_dicts: List[Dict[str, Any]],
    wiki_refs: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Parse [S#]/[M#]/[W#] labels in each sentence and map them to source objects.

    Returns per_claim_sources: {sentence_text: [source_dicts]} for sentences that
    have at least one citation. Keys are citation-stripped so they match the
    sentence text produced by extract_entities_from_text when citations are
    pre-stripped from the assistant answer before extraction.

    Citation-only trailing segments (e.g. "[S1]" appearing as a standalone
    sentence after splitting on sentence boundaries) are merged back into the
    preceding factual sentence so their citations are properly attributed.
    """
    mem_by_num: Dict[str, Dict[str, Any]] = {}
    for m in memories_as_dicts:
        label = m.get("memory_label", "")
        if label.startswith("M") and label[1:].isdigit():
            mem_by_num[label[1:]] = m

    wiki_by_num: Dict[str, Dict[str, Any]] = {}
    for w in wiki_refs:
        label = w.get("wiki_label", "")
        if label.startswith("W") and label[1:].isdigit():
            wiki_by_num[label[1:]] = w

    # Pattern that matches a citation block at the very start of a segment,
    # e.g. "[S1] " or "[S1][M2] " before any factual text.
    _LEADING_CITES_RE = re.compile(r"^((?:\[(?:S|M|W)\d+\]\s*)+)(.*)", re.DOTALL)

    result: Dict[str, List[Dict[str, Any]]] = {}
    raw_segments = re.split(r"(?<=[.!?])\s+", answer.strip())

    # Build groups: (key_text, [(prefix, num_str), ...])
    # Citations at the START of a segment after a sentence-boundary split are
    # trailing citations for the PREVIOUS sentence (the split consumed the period
    # and following space, leaving "[S1]" stranded at the head of the next chunk).
    # Citations inline in or at the end of the factual content belong to the
    # current sentence.
    groups: List[tuple] = []  # (key_text, [(prefix, num_str)])

    for seg in raw_segments:
        seg = seg.strip()
        if not seg:
            continue

        # Peel off any leading citation block.
        lm = _LEADING_CITES_RE.match(seg)
        if lm:
            leading_part = lm.group(1).strip()
            factual_part = lm.group(2).strip()
        else:
            leading_part = ""
            factual_part = seg

        # Attribute leading citations to the PREVIOUS group (they are trailing
        # citations that landed in front of the next sentence after the split).
        if leading_part and groups:
            prev_key, prev_cites = groups[-1]
            groups[-1] = (prev_key, prev_cites + _CITE_RE.findall(leading_part))

        # Process the factual content of this segment.
        key = _CITE_STRIP.sub("", factual_part).strip()
        # Mirror the punctuation-space normalization applied in compile_query_job
        # so keys match the claim_text stored by the compiler.
        key = re.sub(r"\s+([.!?,;:])", r"\1", key)
        if not key:
            # No factual content — all citations already attributed above.
            continue

        factual_cites = _CITE_RE.findall(factual_part)
        groups.append((key, factual_cites))

    def _resolve_cite(prefix: str, num_str: str) -> "Optional[Dict[str, Any]]":
        idx = int(num_str) - 1
        if prefix == "S" and 0 <= idx < len(doc_sources):
            src = dict(doc_sources[idx])
            src["source_kind"] = "document"
            return src
        if prefix == "M" and num_str in mem_by_num:
            mem = dict(mem_by_num[num_str])
            mem["source_kind"] = "memory"
            raw_id = mem.get("memory_id") or mem.get("id")
            mem["memory_id"] = int(raw_id) if str(raw_id).isdigit() else None
            return mem
        if prefix == "W" and num_str in wiki_by_num:
            ref = dict(wiki_by_num[num_str])
            ref["source_kind"] = "wiki"
            return ref
        return None

    for key, cites in groups:
        if not cites:
            continue
        sources_for = [s for s in (_resolve_cite(p, n) for p, n in cites) if s is not None]
        if sources_for:
            result[key] = sources_for

    return result
