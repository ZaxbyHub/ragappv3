#!/usr/bin/env python
"""Standalone validator for the 2026-09 calibration artifacts (issue #36).

Python 3.9+ stdlib only. Exit 0 iff the requested scope validates.

Scopes:
  dataset        frozen calibration dataset integrity (AC1)
  distributions  per-score-type raw distributions + summary stats (AC2)
  cuts           derived cut points, held-out rates, cross-file constants (AC3)
  e05            E05 ablation evidence doc (AC7)
  perf           concurrency/performance evidence doc (AC8)
  research       model-research verification note (AC9)
  release        rollout/rollback release note (AC10)

Missing files are reported as ``MISSING: <path>`` and fail the scope.
"""

import json
import re
import sys
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
# HERE = backend/tests/eval/calibration_2026_09 -> repo root is 4 levels up.
REPO_ROOT = HERE.parents[3]

QUERIES = HERE / "calibration_queries.jsonl"
SCORE_DUMP = HERE / "score_dump.json"
ANALYSIS = HERE / "analysis.json"
CUTOFF_SWEEP = HERE / "cutoff_sweep.json"

DOC_E05 = REPO_ROOT / "docs" / "eval" / "2026-09-model-qualification.md"
DOC_E05_DATA = REPO_ROOT / "docs" / "eval" / "2026-09-model-qualification-data.json"
DOC_PERF = REPO_ROOT / "docs" / "eval" / "2026-09-performance.md"
DOC_PERF_DATA = REPO_ROOT / "docs" / "eval" / "2026-09-performance-data.json"
DOC_RESEARCH = REPO_ROOT / "docs" / "eval" / "2026-09-model-research.md"
DOC_RELEASE = REPO_ROOT / "docs" / "releases" / "pending" / "36-f2-model-qualification.md"

RELEVANCE_TEST = REPO_ROOT / "frontend" / "src" / "lib" / "relevance.test.ts"

REQUIRED_CLASSES = {
    "exact-identifier",
    "paraphrase",
    "numeric-fact",
    "procedure",
    "no-answer",
}

# Frozen calibrated constants (see README.md; spec must not drift).
DISTANCE_CUTS = {"hr_re": 0.56, "re_rl": 0.67, "rl_ta": 0.77}
RERANK_CUTS = {"hr_re": 0.7, "re_rl": 0.4, "rl_ta": 0.2}
CUT_TOLERANCE = 0.011  # rounding tolerance between raw quantiles and bands


class Missing(Exception):
    def __init__(self, path):
        self.path = path
        super().__init__(str(path))


def read_text(path: Path) -> str:
    if not path.is_file():
        raise Missing(path)
    return path.read_text(encoding="utf-8")


def read_json(path: Path):
    if not path.is_file():
        raise Missing(path)
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_queries() -> List[dict]:
    rows = []
    for line in read_text(QUERIES).splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def check_dataset() -> List[str]:
    problems = []
    queries = load_queries()
    if len(queries) < 50:
        problems.append("dataset has %d queries, need >= 50" % len(queries))
    classes = set()
    splits = set()
    for q in queries:
        for field in ("id", "query", "class", "split"):
            if not isinstance(q.get(field), str) or not q.get(field):
                problems.append("query %r missing non-empty %r" % (q.get("id"), field))
        if q.get("vault_id") is None:
            problems.append("query %r missing vault_id" % q.get("id"))
        cls = q.get("class")
        if isinstance(cls, str):
            classes.add(cls)
            if cls != "no-answer" and not q.get("gold_file_ids"):
                problems.append("content query %r has no gold_file_ids" % q.get("id"))
        sp = q.get("split")
        if isinstance(sp, str):
            splits.add(sp)
    missing_classes = REQUIRED_CLASSES - classes
    if missing_classes:
        problems.append("missing required classes: %s" % sorted(missing_classes))
    if len(classes) < 5:
        problems.append("only %d distinct classes, need >= 5" % len(classes))
    for split in ("tuning", "held-out"):
        if split not in splits:
            problems.append("split %r absent or empty" % split)
    # Per-vault balance gate (plan-critic round 1): every representative vault
    # with sampled files carries content queries proportional to its file
    # count — vaults 2 (CDP, 15 files) and 7 (Legacy, 64 files) need >= 5;
    # vault 6 (Slack) holds a single file, so >= 1 is proportional. Vault 1
    # (Default) is empty on the deployment and vault 5 (Test) is excluded as
    # non-representative.
    content_per_vault = {}
    for q in queries:
        if q.get("class") != "no-answer":
            content_per_vault[q.get("vault_id")] = (
                content_per_vault.get(q.get("vault_id"), 0) + 1)
    for vid, minimum in ((2, 5), (7, 5), (6, 1)):
        got = content_per_vault.get(vid, 0)
        if got < minimum:
            problems.append(
                "vault %s has only %d content queries, need >= %d" % (vid, got, minimum))
    return problems


def check_distributions() -> List[str]:
    problems = []
    dump = read_json(SCORE_DUMP)
    meta = dump.get("meta") or {}
    if meta.get("embedding_model") != "microsoft/harrier-oss-v1-0.6b":
        problems.append("meta.embedding_model != microsoft/harrier-oss-v1-0.6b")
    if meta.get("reranker_model") != "BAAI/bge-reranker-v2-m3":
        problems.append("meta.reranker_model != BAAI/bge-reranker-v2-m3")
    results = dump.get("results")
    if not isinstance(results, list) or len(results) != 63:
        problems.append("score_dump has %s results, need exactly 63"
                        % (len(results) if isinstance(results, list) else "no"))
    else:
        for r in results:
            retrieval = r.get("retrieval")
            if not isinstance(retrieval, list) or not retrieval:
                problems.append("result %r has no retrieval list" % r.get("id"))
            else:
                for entry in retrieval:
                    if not isinstance(entry, dict) or "distance" not in entry:
                        problems.append(
                            "result %r retrieval entry without distance" % r.get("id"))
                        break
            rerank = r.get("rerank")
            if not isinstance(rerank, list) or not rerank:
                problems.append("result %r has no rerank list" % r.get("id"))
            else:
                for entry in rerank:
                    if not isinstance(entry, dict) or not isinstance(
                            entry.get("rerank"), (int, float)):
                        problems.append(
                            "result %r rerank entry without numeric score" % r.get("id"))
                        break
    analysis = read_json(ANALYSIS)
    tuning_stats = analysis.get("tuning_stats") or {}
    for key in ("d_gold", "d_noans", "r_gold", "r_noans", "f_gold", "f_noans"):
        if key not in tuning_stats:
            problems.append("analysis.tuning_stats missing %r" % key)
    # Recency-ON RRF supplement (final-critic round 1): AC2 requires the rrf
    # distribution in BOTH forms. The frozen score_dump carries the raw
    # (recency-off) form actually produced by the deployed pipeline; the
    # supplement file carries the recency-blended form (rrf_fuse with
    # recency_scores from file-level processed_at, weight 0.1).
    rec = HERE / "rrf_recency_dump.json"
    if not rec.is_file():
        problems.append("MISSING: %s" % rec)
        print("MISSING: %s" % rec)
    else:
        rd = json.loads(rec.read_text(encoding="utf-8"))
        rows = rd.get("results") or []
        if len(rows) != 63:
            problems.append("rrf_recency_dump has %d results, need 63" % len(rows))
        summary = rd.get("summary") or {}
        for side in ("raw_all_items", "recency_blended_all_items"):
            st = summary.get(side) or {}
            for field in ("n", "min", "max", "q25", "q50", "q75", "q95"):
                if not isinstance(st.get(field), (int, float)):
                    problems.append("rrf_recency_dump.summary.%s.%s missing or non-numeric" % (side, field))
        blended = [i.get("rrf_recency") for r in rows if r.get("items")
                   for i in r["items"][:3] if i.get("rrf_recency") is not None]
        if len(blended) < 100:
            problems.append("rrf_recency_dump has only %d blended values" % len(blended))
        else:
            top_mean = sum(blended) / len(blended)
            if not (0.5 <= top_mean <= 1.0):
                problems.append(
                    "recency-blended top-3 mean %.3f outside the expected [0.5, 1.0] "
                    "normalized band (raw-scale values indicate the blend did not apply)" % top_mean)

    return problems


def _standalone_number(text: str, literal: str) -> bool:
    """True if ``literal`` occurs in ``text`` as a standalone numeric token."""
    pattern = r"(?<![\d.])" + re.escape(literal) + r"(?![\d])"
    return re.search(pattern, text) is not None


def check_cuts() -> List[str]:
    problems = []
    analysis = read_json(ANALYSIS)
    cuts = analysis.get("derived_cuts") or {}
    for family, expected in (("distance", DISTANCE_CUTS), ("rerank", RERANK_CUTS)):
        got = cuts.get(family) or {}
        for key, want in expected.items():
            value = got.get(key)
            if not isinstance(value, (int, float)) or abs(float(value) - want) > CUT_TOLERANCE:
                problems.append(
                    "derived_cuts.%s.%s = %r, expected %.3f +/- %.3f"
                    % (family, key, value, want, CUT_TOLERANCE))
    heldout = analysis.get("heldout_validation") or {}
    new_rate = (heldout.get("distance_new_cuts") or {}).get("rate")
    if not isinstance(new_rate, (int, float)) or new_rate < 0.7:
        problems.append("heldout distance_new_cuts.rate = %r, need >= 0.7" % new_rate)
    old_rr = (heldout.get("rerank_old_cuts") or {}).get("rate")
    if not isinstance(old_rr, (int, float)) or old_rr < 0.8:
        problems.append("heldout rerank_old_cuts.rate = %r, need >= 0.8" % old_rr)
    # Cross-file: analysis cut values (rounded) equal the constants the frozen
    # frontend spec test asserts.
    test_text = read_text(RELEVANCE_TEST)
    for family, expected in (("distance", DISTANCE_CUTS), ("rerank", RERANK_CUTS)):
        got = cuts.get(family) or {}
        for key, want in expected.items():
            value = got.get(key)
            if (isinstance(value, (int, float))
                    and round(float(value), 2) != round(want, 2)):
                problems.append(
                    "derived_cuts.%s.%s rounds to %.2f, not the spec band %.2f"
                    % (family, key, float(value), want))
            if not _standalone_number(test_text, repr(want)):
                problems.append(
                    "relevance.test.ts does not assert the %s band constant %s"
                    % (family, repr(want)))
    # Cutoff-impact provenance (reviewer round-4 L4-3): the prose numbers
    # (0.236 -> 0.909, ceiling 0.927, leak 16.7%) must exist in the generated
    # cutoff_sweep.json artifact, not only in prose.
    if not CUTOFF_SWEEP.is_file():
        problems.append("MISSING: %s (run generate_cutoff_sweep.py)" % CUTOFF_SWEEP)
        print("MISSING: %s" % CUTOFF_SWEEP)
    else:
        sweep = json.loads(CUTOFF_SWEEP.read_text(encoding="utf-8"))
        expected = {"0.5": 0.236, "0.75": 0.909}
        for t, want in expected.items():
            got = (sweep.get("thresholds") or {}).get(t, {}).get("gold_recall_at_7")
            if got != want:
                problems.append(
                    "cutoff_sweep.thresholds.%s.gold_recall_at_7 = %r, expected %r"
                    % (t, got, want))
        ceil = (sweep.get("ceiling_no_filter") or {}).get("gold_recall_at_7")
        if ceil != 0.927:
            problems.append("cutoff_sweep ceiling %r != 0.927" % ceil)
        leak = (sweep.get("no_answer_leak_at_075") or {}).get("rate")
        if leak != 0.167:
            problems.append("cutoff_sweep no_answer_leak_at_075 %r != 0.167" % leak)
    return problems


def _sections(text: str):
    """Split markdown into (heading, body) pairs; body excludes the heading."""
    lines = text.splitlines()
    sections = []
    current_heading = ""
    body: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            sections.append((current_heading, "\n".join(body)))
            current_heading = stripped
            body = []
        else:
            body.append(line)
    sections.append((current_heading, "\n".join(body)))
    return [(h, b) for h, b in sections if h]


def _heading_matches(heading: str, marker: str) -> bool:
    if not heading.startswith(marker):
        return False
    rest = heading[len(marker):]
    return not (rest and rest[0].isdigit())


def check_markdown_doc(path: Path, markers: List[str], extra=None) -> List[str]:
    problems = []
    text = read_text(path)
    sections = _sections(text)
    for marker in markers:
        bodies = [b for h, b in sections if _heading_matches(h, marker)]
        if not bodies:
            problems.append("missing section marker %r" % marker)
        elif not any(b.strip() for b in bodies):
            problems.append("section %r is empty" % marker)
    if extra:
        problems.extend(extra(text))
    return problems


def check_e05() -> List[str]:
    problems = []

    def raw_data_line(text: str) -> List[str]:
        issues = []
        raw_line = None
        for line in text.splitlines():
            if "raw-data:" in line:
                raw_line = line
                break
        if raw_line is None:
            issues.append("no 'raw-data:' line")
        else:
            match = re.search(r"raw-data:\s*(\S+\.json)", raw_line)
            if not match:
                issues.append("'raw-data:' line does not name a .json file")
            else:
                target = (DOC_E05.parent / match.group(1)).resolve()
                if not target.is_file():
                    issues.append("MISSING: %s" % target)
                    print("MISSING: %s" % target)
                elif target.name != DOC_E05_DATA.name:
                    issues.append(
                        "raw-data target %s is not %s" % (target.name, DOC_E05_DATA.name))
        return issues

    problems.extend(check_markdown_doc(DOC_E05, [
        "## Harrier model-card verification",
        "## Reranker A/B (frozen pool)",
        "## Contextual chunking ablation",
        "## Top-k / top-n grid",
        "## Negative results",
    ], extra=raw_data_line))
    problems.extend(_check_e05_data())
    emb = REPO_ROOT / "docs" / "eval" / "2026-09-model-qualification-data.json"
    if emb.is_file():
        ed = json.loads(emb.read_text(encoding="utf-8"))
        c36 = ed.get("chunking36") or {}
        for side in ("off", "on"):
            node = c36.get(side) or {}
            val = node.get("recall_at_7")
            if not isinstance(val, (int, float)):
                problems.append("chunking36.%s.recall_at_7 is not numeric" % side)
            elif not (0.0 <= float(val) <= 1.0):
                problems.append(
                    "chunking36.%s.recall_at_7 = %r outside [0, 1]" % (side, val))
        if c36.get("per_query_agreement") != "55/55":
            problems.append("chunking36.per_query_agreement must be '55/55' (full-corpus tie)")
    if emb.is_file():
        ed = json.loads(emb.read_text(encoding="utf-8"))
        ec = ed.get("embedding_challenger") or {}
        if not ec.get("per_file_failures"):
            problems.append("qualification data missing embedding_challenger.per_file_failures")
        if not ec.get("attempts"):
            problems.append("qualification data missing embedding_challenger.attempts")
    else:
        problems.append("MISSING: %s" % emb)
        print("MISSING: %s" % emb)
    return problems


def _numeric(obj, *path):
    """Return obj at path if it is a finite number, else None."""
    node = obj
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, (int, float)) else None


def _check_e05_data() -> List[str]:
    """Content gate (plan-critic round 1): the raw-data JSON must carry real
    per-arm metrics — heading-typing alone cannot pass AC7."""
    problems = []
    try:
        data = read_json(DOC_E05_DATA)
    except Missing as exc:
        return ["MISSING: %s" % exc.path]
    arms = ("prefix_ab", "reranker_ab", "chunking", "topk_grid")
    for arm in arms:
        if arm not in data or not isinstance(data.get(arm), dict):
            problems.append("e05 data missing arm %r" % arm)
    if problems:
        return problems
    for key in ("recall_at_7",):
        for side in ("baseline", "prefixed"):
            if _numeric(data, "prefix_ab", side, key) is None:
                problems.append(
                    "prefix_ab.%s.%s is not numeric" % (side, key))
    rr = data["reranker_ab"]
    if rr.get("challenger_comparability") not in ("tei", "ad-hoc-cpu", "ad-hoc-gpu", "failed"):
        problems.append("reranker_ab.challenger_comparability must be "
                        "'tei'|'ad-hoc-cpu'|'failed'")
    if not isinstance(rr.get("challenger_model"), str) or not rr.get("challenger_model"):
        problems.append("reranker_ab.challenger_model missing")
    if _numeric(rr, "baseline", "recall_at_7") is None:
        problems.append("reranker_ab.baseline.recall_at_7 is not numeric")
    if (rr.get("challenger_comparability") == "failed"
            and _numeric(rr, "challenger", "recall_at_7") is not None):
        problems.append("reranker_ab claims 'failed' but carries challenger numbers")
    if (rr.get("challenger_comparability") != "failed"
            and _numeric(rr, "challenger", "recall_at_7") is None):
        problems.append("reranker_ab.challenger.recall_at_7 is not numeric")
    chunk = data["chunking"]
    if not isinstance(chunk.get("verdict"), str) or not chunk.get("verdict"):
        problems.append("chunking.verdict missing")
    for side in ("off", "on"):
        if _numeric(chunk, side, "recall_at_7") is None:
            problems.append("chunking.%s.recall_at_7 is not numeric" % side)
    grid = data["topk_grid"]
    configs = grid.get("configs")
    if not isinstance(configs, list) or len(configs) < 4:
        problems.append("topk_grid.configs needs >= 4 entries")
    else:
        for cfg in configs:
            if _numeric(cfg, "recall_at_7") is None:
                problems.append("topk_grid config without numeric recall_at_7")
                break
    return problems


def check_perf() -> List[str]:
    problems = []

    def percentile_mentions(text: str) -> List[str]:
        issues = []
        low = text.lower()
        for token in ("p50", "p95"):
            if token not in low:
                issues.append("no %s mention" % token)
        return issues

    problems.extend(check_markdown_doc(DOC_PERF, [
        "## Methodology",
        "## Concurrency 1",
        "## Concurrency 4",
        "## Concurrency 8",
        "## Concurrency 12",
        "## Long generation",
        "## Bulk ingestion",
    ], extra=percentile_mentions))
    if not DOC_PERF_DATA.is_file():
        problems.append("MISSING: %s" % DOC_PERF_DATA)
        print("MISSING: %s" % DOC_PERF_DATA)
        return problems
    # Content gate (plan-critic round 1): numeric p50/p95 with sample counts
    # per concurrency tier, plus the long-generation and bulk-ingest entries —
    # heading-typing alone cannot pass AC8.
    try:
        data = read_json(DOC_PERF_DATA)
    except Missing as exc:
        return ["MISSING: %s" % exc.path]
    tiers = data.get("tiers")
    if not isinstance(tiers, dict):
        return ["perf data missing 'tiers' object"]
    for tier in ("1", "4", "8", "12"):
        node = tiers.get(tier)
        if not isinstance(node, dict):
            problems.append("perf data missing tier %r" % tier)
            continue
        for field in ("samples", "queue_wait_p50_ms", "queue_wait_p95_ms",
                      "first_content_p50_ms", "first_content_p95_ms",
                      "completion_p50_ms", "completion_p95_ms"):
            if _numeric(node, field) is None:
                problems.append("perf tier %r field %r is not numeric" % (tier, field))
        for field in ("errors", "empty_turns"):
            if not isinstance(node.get(field), int):
                problems.append("perf tier %r field %r is not an integer" % (tier, field))
    # Measured stage spans (user-ordered instrumentation): the app's own SSE
    # `stage` events + /metrics deltas — the derived milestone proxy is superseded.
    spans = data.get("stage_spans")
    if not isinstance(spans, dict):
        problems.append("perf data missing 'stage_spans'")
    else:
        for tier in ("1", "4", "8", "12"):
            node = spans.get(tier) or {}
            ss = node.get("stage_spans_s") or {}
            for span in ("admission_to_searching", "searching_to_reading",
                         "reading_to_drafting", "drafting_to_done"):
                entry = ss.get(span) or {}
                if not isinstance(entry.get("p50"), (int, float)) or not isinstance(entry.get("p95"), (int, float)):
                    problems.append(
                        "perf tier %r stage span %r missing numeric p50/p95" % (tier, span))
                    break
    for probe in ("long_generation", "bulk_ingestion"):
        node = data.get(probe)
        if not isinstance(node, dict):
            problems.append("perf data missing %r" % probe)
            continue
        for field in ("samples", "p50_ms", "p95_ms"):
            if _numeric(node, field) is None:
                problems.append("perf %r field %r is not numeric" % (probe, field))
    return problems


def check_research() -> List[str]:
    problems = []

    def urls_and_dates(text: str) -> List[str]:
        issues = []
        urls = re.findall(r"https://\S+", text)
        if len(urls) < 3:
            issues.append("only %d https:// URLs cited, need >= 3" % len(urls))
        hosts = set()
        for url in urls:
            match = re.match(r"https://([^/\s]+)", url)
            if match:
                hosts.add(match.group(1).lower())
        if len(hosts) < 3:
            issues.append(
                "URLs cite only %d distinct primary hosts, need >= 3" % len(hosts))
        dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text)
        if len(dates) < 3:
            issues.append("only %d access dates (YYYY-MM-DD), need >= 3" % len(dates))
        elif len(set(dates)) < 3:
            issues.append("access dates are not distinct (need >= 3 distinct)")
        return issues

    problems.extend(check_markdown_doc(DOC_RESEARCH, [
        "## Harrier query/document formatting",
        "## Harrier query prefix",
        "## Challenger verification",
    ], extra=urls_and_dates))
    return problems


def check_release() -> List[str]:
    problems = []

    def no_global_flips(text: str) -> List[str]:
        issues = []
        if "no global model defaults changed in this pr" not in text.lower():
            issues.append("missing the 'no global model defaults changed in this PR' phrase")
        # Carve-out gate (plan-critic round 1): the one calibrated default that
        # DID change must be named with its before/after values, so the
        # no-flip phrase cannot stand alone as a literal falsehood.
        low = text.lower()
        for token in ("max_distance_threshold", "0.5", "0.75"):
            if token not in low:
                issues.append(
                    "release note lacks the max_distance_threshold carve-out "
                    "token %r" % token)
        return issues

    problems.extend(check_markdown_doc(DOC_RELEASE, [
        "## Rollout",
        "## Rollback",
    ], extra=no_global_flips))
    return problems


SCOPES = {
    "dataset": check_dataset,
    "distributions": check_distributions,
    "cuts": check_cuts,
    "e05": check_e05,
    "perf": check_perf,
    "research": check_research,
    "release": check_release,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    scope = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--scope":
            if i + 1 >= len(argv):
                print("FAIL: --scope requires a value")
                return 2
            scope = argv[i + 1]
            i += 2
        elif argv[i].startswith("--scope="):
            scope = argv[i].split("=", 1)[1]
            i += 1
        else:
            rest.append(argv[i])
            i += 1
    if rest:
        print("FAIL: unknown arguments: %s" % " ".join(rest))
        return 2
    if scope not in SCOPES:
        print("FAIL: unknown scope %r; expected one of %s"
              % (scope, "|".join(sorted(SCOPES))))
        return 2
    try:
        problems = SCOPES[scope]()
    except Missing as exc:
        print("MISSING: %s" % exc.path)
        print("FAIL: %s — required artifact missing" % scope)
        return 1
    if problems:
        shown = "; ".join(problems[:5])
        more = "" if len(problems) <= 5 else " (+%d more)" % (len(problems) - 5)
        print("FAIL: %s — %s%s" % (scope, shown, more))
        return 1
    print("PASS: %s" % scope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
