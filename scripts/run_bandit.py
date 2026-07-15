#!/usr/bin/env python3
"""Run the RAGAPPv3 backend SAST gate (bandit) or regenerate its baseline.

This is the single entrypoint for SAST in CI and local development.

Usage
-----
Run the gated scan (what CI runs)::

    python scripts/run_bandit.py

    Exits 0 when the only findings are already in the committed baseline
    (i.e. no NEW findings). Exits non-zero when a NEW finding appears, or
    when bandit itself errors. The committed baseline lives at
    ``backend/security/bandit-baseline.json``.

Regenerate the committed baseline (run after intentionally accepting new
pre-existing findings)::

    python scripts/run_bandit.py --update-baseline

    Writes a fresh, normalized baseline to
    ``backend/security/bandit-baseline.json`` and prints a diff of the
    finding IDs that were added or removed relative to the previous baseline
    so a reviewer can see exactly what the bump suppresses.

Why a wrapper instead of raw ``bandit --baseline`` in CI
-------------------------------------------------------
Bandit compares findings for equality using the ``filename`` field as an exact
string (``bandit/core/issue.py`` ``Issue.__eq__``), with no path normalization
on load. On Windows, bandit records backslash paths (``backend\\app\\...``);
on Linux (our CI) it records forward-slash paths. A baseline committed with one
separator therefore fails spuriously on the other platform. This wrapper
normalizes every baseline filename to forward slashes so the committed file is
cross-platform stable, and strips the volatile ``generated_at`` timestamp so
regeneration is deterministic.

Run from the repository root.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# All paths are relative to the repository root (run_bandit.py lives in scripts/).
ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
CONFIG = BACKEND / ".bandit"
# Bandit records the path it is given as each finding's ``filename``. Pass a
# repo-relative target so the baseline stores portable paths (e.g.
# ``backend/app/...``) rather than a machine-specific absolute path. Separators
# are normalized to forward slashes in ``_normalize``.
TARGET = "backend/app"
BASELINE = BACKEND / "security" / "bandit-baseline.json"


def _run_bandit_json(target: str, extra: list[str]) -> tuple[int, dict | None]:
    """Run bandit with JSON output to a temp file; return (exit_code, parsed_json).

    bandit exits non-zero when it finds issues, which is expected when generating a
    baseline. We only treat an *unparseable* result as a hard error. A bounded
    ``timeout`` prevents a pathological scan (symlink loop, AST explosion) from
    hanging the gate until the outer CI job timeout. The temp file is written to
    the system temp dir (not the repo root) so a hard kill cannot leak a file into
    the working tree that could be accidentally staged.
    """
    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=".json", delete=False
    ) as tmp:
        out_path = tmp.name
    try:
        cmd = [
            sys.executable,
            "-m",
            "bandit",
            "-c",
            str(CONFIG),
            "-r",
            target,
            "-f",
            "json",
            "-o",
            out_path,
            *extra,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=300
            )
        except subprocess.TimeoutExpired:
            sys.stderr.write(
                "run_bandit: bandit scan timed out after 300s — "
                "possible symlink loop or pathological input\n"
            )
            return 1, None
        try:
            with open(out_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            # ValueError covers JSONDecodeError and UnicodeDecodeError.
            sys.stderr.write(proc.stdout + proc.stderr)
            sys.stderr.write(f"run_bandit: failed to parse bandit JSON ({exc})\n")
            return proc.returncode or 1, None
        # Re-emit bandit's human-readable output for visibility.
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        return proc.returncode, data
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _normalize_for_commit(data: dict) -> dict:
    """Strip volatile fields and normalize all paths to forward slashes.

    The committed baseline is platform-neutral (forward slashes) so it is
    diff-stable and reviewable. This is what is written to
    ``backend/security/bandit-baseline.json``. Both finding ``filename`` fields
    and the per-file keys under ``metrics`` are normalized, since bandit emits
    OS-native separators in both places on Windows.
    """
    data.pop("generated_at", None)
    for result in data.get("results", []):
        fname = result.get("filename", "")
        if fname:
            result["filename"] = fname.replace("\\", "/")
    metrics = data.get("metrics", {})
    if isinstance(metrics, dict):
        data["metrics"] = {
            (k.replace("\\", "/") if isinstance(k, str) else k): v
            for k, v in metrics.items()
        }
    return data


def _norm_path(fname: str) -> str:
    """Normalize a finding path to forward slashes for stable cross-platform keys."""
    return fname.replace("\\", "/") if fname else fname


def _finding_key(result: dict) -> str:
    """A stable, human-readable key for one finding, used for diffing baselines.

    Path separators are normalized to forward slashes so the same finding produces
    the same key on Windows and Linux (bandit emits OS-native separators, but the
    underlying finding is identical).

    The key INCLUDES the line number. A given file often has multiple findings of
    the same ``test_id`` (e.g. ``deps.py`` has four B608 SQL-query findings), and
    their ``issue_text`` is identical, so a key of just ``(test_id, filename)``
    would collapse them into one — silently suppressing a genuinely NEW finding
    of that test_id anywhere else in the file. Including the line number means a
    newly introduced finding on a previously-clean line is correctly reported as
    new. The cost is that an edit which shifts an existing finding's line number
    shows as removed+added; that churn is the designed signal to re-run
    ``--update-baseline`` (the finding is unchanged, just moved), which is the
    intended workflow.
    """
    return (
        f"{result.get('test_id', '?')} "
        f"{_norm_path(result.get('filename', '?'))}:{result.get('line_number', '?')}"
    )


def _load_baseline_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {_finding_key(r) for r in data.get("results", [])}
    except (OSError, ValueError) as exc:
        # A corrupt prior baseline makes the update diff treat every current
        # finding as ADDED. That is noisy (not silent), but surface the cause so
        # the author knows to investigate rather than misread the diff.
        sys.stderr.write(
            f"run_bandit: prior baseline at {path} is unreadable ({exc}); "
            f"diff will show all current findings as added\n"
        )
        return set()


def update_baseline() -> int:
    """Regenerate the committed baseline with normalized paths + no timestamp."""
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    prev_keys = _load_baseline_keys(BASELINE)
    exit_code, data = _run_bandit_json(TARGET, [])
    if data is None:
        sys.stderr.write("run_bandit: failed to generate baseline JSON\n")
        return 1
    data = _normalize_for_commit(data)
    with open(BASELINE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")

    new_keys = {_finding_key(r) for r in data.get("results", [])}
    added = sorted(new_keys - prev_keys)
    removed = sorted(prev_keys - new_keys)
    total = len(data.get("results", []))
    sys.stdout.write(
        f"run_bandit: baseline written to {BASELINE.relative_to(ROOT)} "
        f"({total} findings)\n"
    )
    if added:
        sys.stdout.write(
            f"run_bandit: {len(added)} finding(s) ADDED to baseline "
            "(newly suppressed — justify in the PR):\n"
        )
        for key in added:
            sys.stdout.write(f"  + {key}\n")
    if removed:
        sys.stdout.write(
            f"run_bandit: {len(removed)} finding(s) REMOVED from baseline "
            "(no longer suppressed):\n"
        )
        for key in removed:
            sys.stdout.write(f"  - {key}\n")
    if not added and not removed and prev_keys:
        sys.stdout.write("run_bandit: no changes vs previous baseline\n")
    # Baseline regeneration is not a CI failure even when bandit found issues.
    return 0


def gated_scan() -> int:
    """Run bandit and fail only on NEW findings not present in the baseline.

    The comparison is done here in Python against normalized keys rather than via
    ``bandit --baseline``: bandit compares findings using the ``filename`` field
    as an exact string (``Issue.__eq__``) with no path normalization, so it emits
    OS-native separators that differ between Windows dev machines and Linux CI.
    Comparing forward-slash-normalized keys here makes the gate match bandit's
    own intent (a finding is "the same" regardless of platform) while keeping the
    committed baseline portable and the runtime check reliable everywhere.
    """
    if not BASELINE.exists():
        sys.stderr.write(
            f"run_bandit: baseline not found at {BASELINE.relative_to(ROOT)}.\n"
            f"Run `python scripts/run_bandit.py --update-baseline` first.\n"
        )
        return 1
    try:
        with open(BASELINE, encoding="utf-8") as fh:
            baseline_data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"run_bandit: baseline at {BASELINE.relative_to(ROOT)} is unreadable "
            f"({exc}). Regenerate with `python scripts/run_bandit.py "
            f"--update-baseline`.\n"
        )
        return 1
    baseline_keys = {_finding_key(r) for r in baseline_data.get("results", [])}

    # A full JSON scan always returns bandit exit 1 when any finding exists; that
    # is expected and is not itself a failure. We decide pass/fail from the diff.
    _exit_code, data = _run_bandit_json(TARGET, [])
    if data is None:
        sys.stderr.write("run_bandit: failed to run bandit for the gated scan\n")
        return 1
    current_results = data.get("results", [])
    current_keys = {_finding_key(r) for r in current_results}
    new_keys = sorted(current_keys - baseline_keys)

    if not new_keys:
        sys.stdout.write(
            f"run_bandit: PASS — no new findings ({len(current_results)} current "
            f"findings, all suppressed by the baseline).\n"
        )
        return 0

    sys.stdout.write(
        f"run_bandit: FAIL — {len(new_keys)} new finding(s) detected:\n"
    )
    # Index current results by key to print locations for the new findings. A key
    # may map to multiple findings (e.g. two issues on one line); report the count
    # so none are hidden by a set/dict collapse.
    by_key: dict[str, list[dict]] = {}
    for r in current_results:
        by_key.setdefault(_finding_key(r), []).append(r)
    for key in new_keys:
        rows = by_key.get(key, [])
        sev = rows[0].get("issue_severity", "?") if rows else "?"
        text = (
            rows[0].get("issue_text", "").strip().splitlines()[0][:100] if rows else ""
        )
        count_note = f" (x{len(rows)})" if len(rows) > 1 else ""
        sys.stdout.write(f"  [{sev}] {key}{count_note} — {text}\n")
    sys.stdout.write(
        "Fix the code, or if the finding is acceptable pre-existing debt, "
        "regenerate the baseline with "
        "`python scripts/run_bandit.py --update-baseline` and justify the "
        "newly-suppressed IDs in the PR.\n"
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Regenerate backend/security/bandit-baseline.json and print a diff.",
    )
    args = parser.parse_args()
    if args.update_baseline:
        return update_baseline()
    return gated_scan()


if __name__ == "__main__":
    raise SystemExit(main())
