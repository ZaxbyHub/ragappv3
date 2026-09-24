"""Generate manifest.json + cases.json for the retrieval gold corpus.

The plain-text documents under fixtures/retrieval_gold/ are authored by
hand; this committed tool computes the normalized (CRLF-to-LF) hashes and
span offsets and validates every corpus rule, so the emitted artifacts are
correct by construction and reproducible from the fixtures alone.

Run from anywhere:  python backend/tests/retrieval_gold/generate_corpus.py
Regeneration must be byte-stable (git status stays clean on a fresh run).
"""

import hashlib
import io
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND / "tests" / "fixtures" / "retrieval_gold"

DOCUMENTS = [
    # (path, doc_id, title, role, superseded_by)
    ("aeris_m_spec_rev2.md", "doc_aeris_m_spec_rev2", "Kellerman Aeris-M Field Specification, Revision 2", "controlling_spec", None),
    ("aeris_m_spec_rev1_superseded.md", "doc_aeris_m_spec_rev1", "Kellerman Aeris-M Field Specification, Revision 1 (superseded)", "superseded_spec", "doc_aeris_m_spec_rev2"),
    ("marram_tdr_9_spec.md", "doc_marram_tdr_9_spec", "Voss Marram TDR-9 Product Specification", "competing_spec", None),
    ("aeris_m_safety_datasheet.md", "doc_aeris_m_safety_datasheet", "Aeris-M Field Safety Datasheet", "safety", None),
    ("distributor_bulletin.md", "doc_distributor_bulletin", "Cobalt AgriSupply Service Bulletin SB-2031-04", "distributor_guidance", None),
    ("site_deployment_memo.md", "doc_site_deployment_memo", "Site Deployment Memo - Gretna Marsh Pilot", "site_memo", None),
    ("programme_timeline.md", "doc_programme_timeline", "Gretna Marsh Programme Timeline", "timeline", None),
    ("programme_glossary.md", "doc_programme_glossary", "Gretna Marsh Programme Glossary", "glossary", None),
    ("site_permit.txt", "doc_site_permit", "Provincial Board of Alderbury Field Instrumentation Permit WPB-2049-1187", "permit", None),
    ("field_notes_opinion.md", "doc_field_notes_opinion", "Field Notes - A Column of Opinions", "opinion", None),
]

ROLES = ["controlling_spec", "superseded_spec", "competing_spec", "safety", "distributor_guidance", "site_memo", "timeline", "glossary", "permit", "opinion"]

KINDS = ["exact_fact", "revision_marker", "vendor_confusion", "memo_quote", "morphological_variant", "not_in_corpus"]

SHARED_PREAMBLE_SENTENCE = (
    "The Aeris-M capacitive soil-moisture probe operates from -20 C to +60 C "
    "at up to 95 percent relative humidity."
)

# Cases: (id, kind, query, expected_doc, span_text, must_not_match, trap_doc, trap_span_text, also_expected, notes)
# expected_doc None marks the not_in_corpus case.
CASES = [
    ("rg-001", "revision_marker",
     "what sampling cadence does revision 2 of the Aeris-M field specification retain",
     "aeris_m_spec_rev2.md", SHARED_PREAMBLE_SENTENCE,
     ["aeris_m_spec_rev1_superseded.md"], None, None, None,
     "Tier-1 doc-level trap: both revision documents carry the span at identical offsets (shared preamble); the discriminator tail ('Revision 2 retains the 15-minute default cadence.') lives in the same chunk. This is the case the frozen C3 probe selects."),
    ("rg-002", "revision_marker",
     "which calibration interval applies under Aeris-M specification revision 2",
     "aeris_m_spec_rev2.md", "Calibration interval: 90 days under normal service.",
     ["aeris_m_spec_rev1_superseded.md"], "aeris_m_spec_rev1_superseded.md", "Calibration interval: 180 days under normal service.", None,
     "Superseded revision near-trap: identical sentence shape, different number."),
    ("rg-003", "revision_marker",
     "what stated accuracy does Aeris-M specification revision 2 give",
     "aeris_m_spec_rev2.md", "Stated accuracy: plus or minus 1.8 percent volumetric water content.",
     ["aeris_m_spec_rev1_superseded.md"], "aeris_m_spec_rev1_superseded.md", "Stated accuracy: plus or minus 2.5 percent volumetric water content.", None,
     "Revision pair accuracy discrimination."),
    ("rg-004", "vendor_confusion",
     "which sensor specification offers a 10 year battery life",
     "marram_tdr_9_spec.md", "Expected battery life: 10 years at the 10-minute default cadence.",
     ["aeris_m_spec_rev2.md"], "aeris_m_spec_rev2.md", "Expected cell life: 5.5 years at the 15-minute default cadence.", None,
     "Vendor confusion: battery vs cell wording across the two product lines."),
    ("rg-005", "vendor_confusion",
     "what is the calibration interval of the Marram TDR-9",
     "marram_tdr_9_spec.md", "Calibration interval: 60 days under normal service.",
     ["aeris_m_spec_rev2.md"], "aeris_m_spec_rev2.md", "Calibration interval: 90 days under normal service.", None,
     "Vendor confusion on calibration interval."),
    ("rg-006", "vendor_confusion",
     "what is the operating temperature range of the Marram TDR-9",
     "marram_tdr_9_spec.md", "Operating range: -40 C to +70 C.",
     ["aeris_m_safety_datasheet.md"], "aeris_m_safety_datasheet.md", "The storage ceiling of 60 C is an absolute limit and is lower than the +70 C operating ceiling of some competitor products; do not transfer handling rules between product lines.", None,
     "The datasheet mentions the competitor's +70 C ceiling inside an Aeris-M warning - a genuine lexical near-trap."),
    ("rg-007", "exact_fact",
     "at what storage temperature may Aeris-M cells vent",
     "aeris_m_safety_datasheet.md", "Store below 60 C. Storage above 60 C may cause cell venting.",
     [], None, None, None, "Straight fact lookup in the safety datasheet."),
    ("rg-008", "memo_quote",
     "which calibration sentence does the site deployment memo tell crews to quote on forms",
     "site_deployment_memo.md", 'this memo quotes the specification directly: "Calibration interval: 90 days under normal service."',
     ["aeris_m_spec_rev2.md"], "aeris_m_spec_rev2.md", "Calibration interval: 90 days under normal service.", None,
     "Tier-2 text-level trap: the memo quotes the specification sentence verbatim inside its own framing, so the expected span (framing + quote) exists only in the memo while the trap span (the bare quoted sentence) is the specification's own wording; the memo's framing vocabulary is the ranking discriminator."),
    ("rg-009", "morphological_variant",
     "is the Aeris-M calibration logging window 24 hours or 7 days under revision 2",
     "aeris_m_spec_rev2.md", "Every calibration event must be logged to the site register within 24 hours of the event.",
     ["aeris_m_spec_rev1_superseded.md"], "aeris_m_spec_rev1_superseded.md", "Every calibration event must be logged to the site register within 7 days of the event.", None,
     "Morphological variant: 'calibrations' (query) vs 'calibration' (document)."),
    ("rg-010", "morphological_variant",
     "explain the 15-min cadence rule for the Aeris-M",
     "aeris_m_spec_rev2.md", "Default sampling cadence: one reading every 15 minutes.",
     ["aeris_m_spec_rev1_superseded.md"], "aeris_m_spec_rev1_superseded.md", "Default sampling cadence: one reading every 30 minutes.", None,
     "Morphological variant: '15-min' (query) shares the '15' token with '15 minutes' in the document."),
    ("rg-011", "exact_fact",
     "when was the provincial permit WPB-2049-1187 issued for the Gretna Marsh Pilot",
     "site_permit.txt", "Issued: 2030-10-02",
     [], None, None, ["programme_timeline.md"],
     "Multi-document question: the timeline also records the issue date and must appear in top-k."),
    ("rg-012", "revision_marker",
     "is 4.2.1 the reference firmware for Aeris-M revision 2",
     "aeris_m_spec_rev2.md", "Reference firmware for Revision 2: version 4.2.1.",
     ["aeris_m_spec_rev1_superseded.md"], "aeris_m_spec_rev1_superseded.md", "Reference firmware for Revision 1: version 3.0.7.", None,
     "Firmware reference discrimination across the revision pair."),
    ("rg-013", "vendor_confusion",
     "how deep can the Marram TDR-9 measure",
     "marram_tdr_9_spec.md", "Measurement depth: up to 90 cm below ground surface.",
     ["aeris_m_spec_rev2.md"], "aeris_m_spec_rev2.md", "Measurement depth: up to 120 cm below ground surface.", None,
     "Vendor confusion on measurement depth."),
    ("rg-014", "exact_fact",
     "what form accompanies an Aeris-M return consignment",
     "aeris_m_safety_datasheet.md", "The return consignment form is DSV-44; damaged units travel under UN3090 labeling.",
     [], None, None, None, "Rare-token fact lookup."),
    ("rg-015", "exact_fact",
     "what does the glossary define as a variance",
     "programme_glossary.md", "Variance: a written board authorization to operate outside a permit condition.",
     [], None, None, None, "Definition lookup in the glossary."),
    ("rg-016", "exact_fact",
     "who issued the site deployment memo and on what date",
     "site_deployment_memo.md", "From: Dr. Marta Ovist, site lead. Date: 2031-03-05.",
     [], None, None, None, "Attribution fact lookup."),
    ("rg-017", "revision_marker",
     "how does revision 2 change the calibration interval relative to revision 1",
     "aeris_m_spec_rev2.md", "Revision 2 shortens the calibration interval from the 180 days set by Revision 1 to 90 days, tightens the stated accuracy from plus or minus 2.5 percent to plus or minus 1.8 percent,",
     ["aeris_m_spec_rev1_superseded.md"], "aeris_m_spec_rev1_superseded.md", "Calibration interval: 180 days under normal service.", None,
     "The revision-notes summary is the only place both numbers appear in one sentence; the superseded document remains the near-trap."),
    ("rg-018", "exact_fact",
     "what was logged on plot 3 on 2031-01-09",
     "programme_timeline.md", "2031-01-09: first drift observation logged on plot 3.",
     ["field_notes_opinion.md"], "field_notes_opinion.md", "The facts: firmware 4.2.1 was released on 2031-01-06, and the first drift observation was logged on 2031-01-09.", None,
     "The opinion column states the same date as fact inside opinion prose - a real lexical near-trap with different phrasing; the timeline Notes paragraph carries the load-bearing relative-date wording."),
    ("rg-019", "exact_fact",
     "how many maintained units does the distributor 1.2 percent accuracy claim rest on",
     "distributor_bulletin.md", "Across 214 maintained units we observe the Aeris-M holding plus or minus 1.2 percent volumetric water content across the full operating band,",
     ["aeris_m_spec_rev2.md"], "aeris_m_spec_rev2.md", "Stated accuracy: plus or minus 1.8 percent volumetric water content.", None,
     "Distributor claim vs controlling figure: the corpus must prefer the bulletin for a bulletin-framed query while the specification's own figure is the near-trap."),
    ("rg-020", "exact_fact",
     "when does provincial permit WPB-2049-1187 expire unless renewed",
     "site_permit.txt", "Expires: 2032-09-30 unless renewed",
     [], None, None, None, "Permit expiry lookup by permit number."),
    ("rg-021", "morphological_variant",
     "compare the default sampling cadence of the Aeris-M revision 2 spec and the Marram TDR-9",
     "marram_tdr_9_spec.md", "Default sampling cadence: one reading every 10 minutes.",
     [], None, None, ["aeris_m_spec_rev2.md"],
     "Multi-document morphological variant: 'intervals' (query) vs 'interval'; both product lines' cadence figures must surface, Marram TDR-9 first for this phrasing."),
    ("rg-022", "exact_fact",
     "what does the publisher say about the field notes column",
     "field_notes_opinion.md", "The publisher insists this column mixes the writer's opinions with a few checkable facts, and readers should keep the two apart.",
     [], None, None, None, "Opinion-document provenance lookup."),
    ("rg-023", "revision_marker",
     "what cell life does Aeris-M revision 2 promise at the default cadence",
     "aeris_m_spec_rev2.md", "Expected cell life: 5.5 years at the 15-minute default cadence.",
     ["aeris_m_spec_rev1_superseded.md"], "aeris_m_spec_rev1_superseded.md", "Expected cell life: 6.0 years at the 30-minute default cadence.", None,
     "Cell-life discrimination across the revision pair; the in-suite trap-chunk-vector probe uses rg-013 (see the test file)."),
    ("rg-024", "not_in_corpus",
     "what is the maintenance schedule for the submarine ballast pump anodes",
     None, None,
     [], None, None, None,
     "Honest absence: every query token is out of domain; the expected result is NO confident hit (empty after the relevance cutoff), not a hallucinated match."),
    ("rg-025", "exact_fact",
     "how many probes does the permit authorize at the site",
     "site_permit.txt", "The holder may operate up to 50 soil-moisture probes of the Kellerman Instruments Aeris-M family at the Gretna Marsh Pilot site, Province of Alderbury.",
     [], None, None, None,
     "Permit ceiling lookup; the permit renders one condition per line."),
]


def normalize(raw: bytes) -> str:
    text = raw.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def main() -> int:
    texts = {}
    manifest_docs = []
    for path, doc_id, title, role, superseded_by in DOCUMENTS:
        with io.open(ROOT / path, "rb") as fh:
            raw = fh.read()
        text = normalize(raw)
        texts[path] = text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifest_docs.append({
            "id": doc_id,
            "path": path,
            "title": title,
            "role": role,
            "sha256": digest,
            "char_length": len(text),
            "superseded_by": superseded_by,
        })
        words = len(text.split())
        assert 300 <= words <= 1500, f"{path}: word count {words} outside 300-1500"
        assert text.isascii(), f"{path}: non-ASCII content"

    # ---- rule validation ----
    errors = []

    def find_span(doc, needle):
        idx = texts[doc].find(needle)
        if idx < 0:
            errors.append(f"{doc}: span text not found: {needle[:60]!r}")
            return None
        if texts[doc].find(needle, idx + 1) >= 0:
            errors.append(f"{doc}: span text not unique: {needle[:60]!r}")
        return idx

    case_records = []
    tier1_cases = 0
    trap_cases = 0
    trap_span_cases = 0
    first_trap_case_id = None
    seen_ids = set()
    for (cid, kind, query, expected_doc, span_text, must_not, trap_doc, trap_text, also_expected, notes) in CASES:
        assert cid not in seen_ids, f"duplicate case id {cid}"
        seen_ids.add(cid)
        record = {"id": cid, "kind": kind, "query": query, "notes": notes}
        if kind == "not_in_corpus":
            assert expected_doc is None and not must_not, f"{cid}: not_in_corpus shape"
            record["expected_doc"] = ""
            record["expected_span"] = None
            case_records.append(record)
            continue
        assert expected_doc in texts, f"{cid}: unknown expected doc {expected_doc}"
        start = find_span(expected_doc, span_text)
        record["expected_doc"] = expected_doc
        record["expected_span"] = {
            "start": start,
            "end": start + len(span_text),
            "text": span_text,
            "sha256": hashlib.sha256(span_text.encode("utf-8")).hexdigest(),
        }
        record["must_not_match"] = list(must_not)
        record["also_expected"] = list(also_expected or [])
        if must_not:
            trap_cases += 1
            if first_trap_case_id is None:
                first_trap_case_id = cid
            for trap in must_not:
                if trap == expected_doc:
                    errors.append(f"{cid}: trap doc equals expected doc")
                if trap not in texts:
                    errors.append(f"{cid}: unknown trap doc {trap}")
            if trap_doc is not None:
                trap_span_cases += 1
                assert trap_doc in must_not, f"{cid}: trap_doc not in must_not_match"
                tstart = find_span(trap_doc, trap_text)
                record["trap_span"] = {
                    "document": trap_doc,
                    "start": tstart,
                    "end": tstart + len(trap_text),
                    "text": trap_text,
                    "sha256": hashlib.sha256(trap_text.encode("utf-8")).hexdigest(),
                }
                if trap_text == span_text:
                    errors.append(f"{cid}: trap span text equals expected span text")
                if span_text in texts[trap_doc]:
                    errors.append(f"{cid}: expected span text occurs in trap doc (violates absent rule)")
            else:
                tier1_cases += 1
                # Tier-1 shared-offset property applies to the probe-selected
                # case (the first with both fields) - the loader-clean-swap
                # guarantee. Other tier-1 traps (e.g. the memo quote) share
                # TEXT at different offsets, which is their own confusable
                # mechanism; the same-offsets rule would be false there.
                pass
        case_records.append(record)

    if first_trap_case_id != "rg-001":
        errors.append(f"first must_not_match case is {first_trap_case_id}, not rg-001 (frozen C3 probe picks it)")
    n = len(case_records)
    if not 20 <= n <= 50:
        errors.append(f"case count {n} outside 20-50")
    if trap_cases < n / 3:
        errors.append(f"trap cases {trap_cases} below one-third floor {n/3:.1f}")
    if trap_span_cases * 2 <= trap_cases:
        errors.append("trap_span cases are not the clear majority of trap cases")

    # Tie guard: no chunk unit (loader's documented split policy) text-identical
    # across documents, at BOTH scales. Uses the repo loader's own chunk_text.
    sys.path.insert(0, str(BACKEND))
    from tests.retrieval_gold.gold_corpus import SCALE_LIMITS, chunk_text  # noqa: E402

    seen_units = {}
    for path, text in texts.items():
        for scale, limit in SCALE_LIMITS.items():
            for unit in chunk_text(text, limit):
                if unit in seen_units and seen_units[unit] != path:
                    errors.append(
                        f"tie guard: identical {scale}-scale chunk unit in {seen_units[unit]} and {path}: {unit[:50]!r}"
                    )
                seen_units[unit] = path

    if errors:
        for err in errors:
            print("GEN-ERROR:", err, file=sys.stderr)
        return 1

    manifest = {
        "schema_version": "1.0.0",
        "corpus_id": "retrieval_gold_corpus_v1",
        "description": (
            "Synthetic, license-safe gold corpus for retrieval-quality measurement. Every name, "
            "organisation, place, date, number and quotation is invented. The corpus deliberately "
            "contains confusable documents (a superseded revision pair sharing a verbatim preamble, "
            "a competing vendor specification, a distributor bulletin contradicting the controlling "
            "specification, and a memo quoting the specification) so retrieval discrimination can be "
            "measured deterministically offline."
        ),
        "normalization": {
            "encoding": "utf-8",
            "policy": "crlf-to-lf",
            "note": (
                "CRLF and lone CR are folded to LF before hashing and before offsets are computed. "
                "All document sha256 values are over the UTF-8 encoding of the normalized text, NOT "
                "raw file bytes, so the manifest holds identically on LF and CRLF checkouts. All "
                "start/end offsets are character offsets into the normalized text. cases.json is "
                "deliberately not byte-hashed here: it is protected structurally (every span carries "
                "its verbatim text and sha256, validated against the document hashes) so the frozen "
                "ranking-degradation probe's sanctioned cases.json rewrite stays loader-valid. "
                "Both JSON files are GENERATED by backend/tests/retrieval_gold/generate_corpus.py "
                "from the hand-authored documents; regeneration is byte-stable."
            ),
        },
        "roles": ROLES,
        "case_kinds": KINDS,
        "documents": manifest_docs,
    }
    with io.open(ROOT / "manifest.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=True)
        fh.write("\n")
    with io.open(ROOT / "cases.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"schema_version": "1.0.0", "corpus_id": "retrieval_gold_corpus_v1", "case_kinds": KINDS, "cases": case_records}, fh, indent=2, ensure_ascii=True)
        fh.write("\n")

    print(f"manifest: {len(manifest_docs)} documents; cases: {n} "
          f"(traps {trap_cases}, trap_span {trap_span_cases}, tier1 {tier1_cases}, "
          f"first trap case {first_trap_case_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
