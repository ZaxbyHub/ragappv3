"""Acceptance check AC9 (issues #947 AC3 / #900 AC4-AC5, collated trace
1171-900-946-947-residuals-hygiene-sweep): the bandit snapshot table in
backend/security/README.md must match the committed
backend/security/bandit-baseline.json.

Root cause (04-root-cause.md RC3): the snapshot table was labelled
point-in-time and never refreshed as the baseline shrank (140 -> 128
results across re-anchoring rounds); nothing compared the table to the
committed baseline, so the drift was invisible. This guard test fills
exactly that hole: it recomputes Total / per-severity / per-band /
files counts from the baseline JSON and requires the README numbers to
agree, keeping the snapshot honest forever.

Frozen Phase 2.5 check (arm's-length author; no fix code existed when
this was written): RED at base (README says Total 140 / 35 files /
B608=59 while the baseline holds 128 results / 30 files / B608=63),
GREEN after the fix refreshes the table.
"""

import json
import re
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECURITY_README = _REPO_ROOT / "backend" / "security" / "README.md"
_BASELINE_JSON = _REPO_ROOT / "backend" / "security" / "bandit-baseline.json"

# ``| HIGH     | 9     |`` / ``| **Total**| **140** |``
_SEVERITY_ROW_RE = re.compile(
    r"^\|\s*\*{0,2}(HIGH|MEDIUM|LOW|Total)\*{0,2}\s*\|"
    r"\s*\*{0,2}(\d+)\*{0,2}\s*\|",
    re.M,
)
# ``| B608 | Hardcoded SQL expressions ... | 59 |`` / ``| others | misc | 6 |``
_BAND_ROW_RE = re.compile(
    r"^\|\s*(B\d{3}|others)\s*\|[^|]+\|\s*(\d+)\s*\|", re.M
)
# ``across 35 files under `backend/app`.``
_FILES_SENTENCE_RE = re.compile(r"across\s+(\d+)\s+files")


def _readme_text() -> str:
    return _SECURITY_README.read_text(encoding="utf-8")


def _parse_severity_rows(text: str) -> dict:
    return {name: int(count) for name, count in _SEVERITY_ROW_RE.findall(text)}


def _parse_band_rows(text: str) -> dict:
    return {name: int(count) for name, count in _BAND_ROW_RE.findall(text)}


def _baseline_counts():
    data = json.loads(_BASELINE_JSON.read_text(encoding="utf-8"))
    assert data.get("errors") == [], (
        f"baseline JSON carries scan errors; counts are meaningless: "
        f"{data['errors']}"
    )
    results = data["results"]
    per_test_id = Counter(r["test_id"] for r in results)
    per_severity = Counter(r["issue_severity"].upper() for r in results)
    files = {r["filename"] for r in results}
    return len(results), per_test_id, per_severity, len(files)


def test_readme_snapshot_matches_baseline():
    """AC9 (#947/#900): every count the security README snapshot prints
    (Total, HIGH/MEDIUM/LOW, per-band family table, files sentence) must
    equal the count derived from the committed bandit baseline.
    RED at base, GREEN after the fix."""
    text = _readme_text()
    readme_sev = _parse_severity_rows(text)
    readme_bands = _parse_band_rows(text)
    files_match = _FILES_SENTENCE_RE.search(text)
    total, per_test_id, per_severity, n_files = _baseline_counts()

    problems = []
    if readme_sev.get("Total") != total:
        problems.append(
            f"Total row says {readme_sev.get('Total')} but the baseline "
            f"holds {total} results"
        )
    for severity in ("HIGH", "MEDIUM", "LOW"):
        if readme_sev.get(severity) != per_severity.get(severity, 0):
            problems.append(
                f"{severity} row says {readme_sev.get(severity)} but the "
                f"baseline holds {per_severity.get(severity, 0)}"
            )
    for band, readme_count in sorted(readme_bands.items()):
        if band == "others":
            expected = total - sum(
                per_test_id.get(other, 0)
                for other in readme_bands
                if other != "others"
            )
        else:
            expected = per_test_id.get(band, 0)
        if readme_count != expected:
            problems.append(
                f"band {band} row says {readme_count} but the baseline "
                f"holds {expected}"
            )
    if files_match is None:
        problems.append(
            "the 'across N files' sentence was not found in the snapshot "
            "paragraph"
        )
    elif int(files_match.group(1)) != n_files:
        problems.append(
            f"files sentence says {files_match.group(1)} but the baseline "
            f"covers {n_files} distinct files"
        )

    assert problems == [], (
        "AC9-README-BANDIT-DRIFT: backend/security/README.md's bandit "
        "snapshot has drifted from backend/security/bandit-baseline.json "
        "(refresh the table and keep it point-in-time-labelled): "
        + "; ".join(problems)
    )


def test_guard_module_parses_snapshot_tables():
    """Collection sanity for this guard module itself: the parsers locate
    the snapshot tables in the README (severity rows, band family rows,
    files sentence), so the drift check above can never pass vacuously by
    parsing nothing. GREEN at base and after the fix."""
    text = _readme_text()
    severity_rows = _parse_severity_rows(text)
    band_rows = _parse_band_rows(text)
    assert {"HIGH", "MEDIUM", "LOW", "Total"} <= set(severity_rows), (
        f"severity table rows not found: {sorted(severity_rows)}"
    )
    assert "B608" in band_rows and "others" in band_rows, (
        f"band family table rows not found: {sorted(band_rows)}"
    )
    assert _FILES_SENTENCE_RE.search(text) is not None, (
        "the 'across N files' sentence was not found"
    )
