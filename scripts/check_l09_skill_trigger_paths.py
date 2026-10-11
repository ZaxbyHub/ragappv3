#!/usr/bin/env python3
"""Issue #780 L09 / AC1 — generated PR-review skill file triggers must reference existing paths.

The opencode-swarm plugin discovers generated skills under
.opencode/skills/generated/*/SKILL.md. A trigger files.include: glob that
matches nothing (issue #780 defect 1: frontend/src/lib/api.ts, deleted by
#295) means the skill never fires for exactly the changes it was generated to
review, and its checklist points reviewers at a deleted file. This check
parses each SKILL.md's YAML frontmatter (the lines between the first two
`---` lines), collects every quoted glob under the trigger: block's
files.include: list, and expands each with glob.glob(..., recursive=True)
resolved from the repo root. Any glob with zero matches prints
"STALE <glob>: <skill-dir-name>" (one line per stale glob) and the script
exits 1; zero stale globs prints "STALE 0" and exits 0. A missing
.opencode/skills/generated/ tree fails closed with exit 2. A SKILL.md with
no trigger/include block is skipped silently.
"""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIR = ROOT / ".opencode" / "skills" / "generated"

LIST_ITEM_RE = re.compile(r"^\s*-\s*[\"']([^\"']+)[\"']\s*$")


def extract_frontmatter(text: str) -> list[str]:
    """Return the lines between the first two `---` markers, else []."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return lines[1:idx]
    return []


def collect_include_globs(frontmatter: list[str]) -> list[str]:
    """Return every quoted glob in the trigger: block's files.include: lists.

    Indent-light state machine: `trigger:` is a top-level key whose value is
    a list of mappings with files.include / files.exclude lists. Only quoted
    list items under an include: key are collected; everything else
    (exclude:, list items outside trigger:, scalar keys) is ignored.
    """
    globs: list[str] = []
    in_trigger = False
    in_include = False
    for line in frontmatter:
        stripped = line.strip()
        if stripped and not line[0].isspace():
            in_trigger = stripped.startswith("trigger:")
            in_include = False
            continue
        if not in_trigger or not stripped:
            continue
        if stripped.startswith("include:"):
            in_include = True
            continue
        if stripped.startswith("exclude:"):
            in_include = False
            continue
        if not in_include:
            continue
        match = LIST_ITEM_RE.match(line)
        if match:
            globs.append(match.group(1))
        else:
            in_include = False
    return globs


def main() -> int:
    if not SCAN_DIR.is_dir():
        print(f"skill-trigger-paths: scanned tree not found: {SCAN_DIR}", file=sys.stderr)
        return 2
    stale: list[str] = []
    try:
        for skill_dir in sorted(p for p in SCAN_DIR.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            frontmatter = extract_frontmatter(skill_md.read_text(encoding="utf-8"))
            for pattern in collect_include_globs(frontmatter):
                if not glob.glob(str(ROOT / pattern), recursive=True):
                    stale.append(f"STALE {pattern}: {skill_dir.name}")
    except OSError as exc:
        print(f"skill-trigger-paths: {exc}", file=sys.stderr)
        return 2

    if stale:
        for line in stale:
            print(line)
        return 1
    print("STALE 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
