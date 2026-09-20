#!/usr/bin/env python
"""Generate cutoff_sweep.json from the frozen score_dump.json (issue #36).

Recomputes the backend distance-cutoff impact numbers quoted in the
calibration README and config docstring (0.236 -> 0.909, ceiling 0.927,
no-answer leak 16.7%) so they are reproducible from committed raws instead
of prose-only. Run:  python generate_cutoff_sweep.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

dump = json.loads((HERE / "score_dump.json").read_text(encoding="utf-8"))
results = dump["results"]


def gold_in_top7(rec, threshold):
    """Gold-file hit within top-7 after applying a distance cutoff."""
    golds = {str(g) for g in rec["gold_file_ids"]}
    kept = [h for h in rec.get("retrieval", [])
            if h.get("distance") is not None and h["distance"] <= threshold]
    return any(str(h["fid"]) in golds for h in kept[:7])


content = [r for r in results if r["class"] != "no-answer"]
noans = [r for r in results if r["class"] == "no-answer"]

sweep = {"thresholds": {}, "meta": {
    "source": "score_dump.json (frozen 63-query live deployment dump)",
    "note": "distance-only (non-reranked) path replay: per-record distance cutoff applied to the retrieved pool, then gold-in-top-7",
}}
for t in (0.5, 0.75):
    hits = sum(1 for r in content if gold_in_top7(r, t))
    sweep["thresholds"][str(t)] = {"gold_recall_at_7": round(hits / len(content), 3)}
ceiling = sum(1 for r in content if gold_in_top7(r, 10.0))
sweep["ceiling_no_filter"] = {"gold_recall_at_7": round(ceiling / len(content), 3)}

na_total = na_pass = 0
for r in noans:
    for h in r.get("retrieval", []):
        if h.get("distance") is None:
            continue
        na_total += 1
        if h["distance"] <= 0.75:
            na_pass += 1
sweep["no_answer_leak_at_075"] = {"results_passed": na_pass, "total": na_total,
                                   "rate": round(na_pass / na_total, 3) if na_total else None}

out = HERE / "cutoff_sweep.json"
out.write_text(json.dumps(sweep, indent=1), encoding="utf-8")
print("wrote", out)
print(json.dumps(sweep["thresholds"]), "ceiling", sweep["ceiling_no_filter"], "leak", sweep["no_answer_leak_at_075"])
