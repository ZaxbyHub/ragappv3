#!/usr/bin/env python3
"""Issue #780 L09 / AC1 — generated PR-review skill file triggers must reference existing paths.

The opencode-swarm plugin discovers generated skills under
.opencode/skills/generated/*/SKILL.md. A trigger files.include: glob that
matches nothing (issue #780 defect 1: frontend/src/lib/api.ts, deleted by
#295) means the skill never fires for exactly the changes it was generated to
review, and its checklist points reviewers at a deleted file. This check
parses each SKILL.md's YAML frontmatter (the lines between the first two
`---` lines), collects every list item under the trigger: block's
files.include: key (quoted or bare, block or flow style), and expands each
with glob.glob(..., recursive=True) resolved from the repo root (the root is
glob.escaped so bracket characters in a checkout path cannot produce a false
STALE). A glob is STALE when it escapes the repository root (absolute or a
`..` segment), matches nothing, or matches only test files (*.test.* /
*.spec.* — the generated skills' own exclude convention). Backtick-quoted
frontend|backend|scripts path references in the SKILL.md BODY must resolve
too, so a stale checklist reference cannot survive a trigger-only fix.

Fail-closed rules (review feedback, PR #880): an `include:` key that yields
zero parseable globs exits 2 (parser drift, not a clean sweep); zero globs
across ALL generated skills exits 2. A missing .opencode/skills/generated/
tree exits 2. Zero violations prints "STALE 0" and exits 0.
"""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIR = ROOT / ".opencode" / "skills" / "generated"

LIST_ITEM_RE = re.compile(r"^\s*-\s*(.+?)\s*$")
FLOW_ITEM_RE = re.compile(r"[\"']([^\"']+)[\"']")
BODY_PATH_RE = re.compile(r"`((?:frontend|backend|scripts)/[^`\s]+)`")
TEST_FILE_RE = re.compile(r"\.(test|spec)\.")


def extract_frontmatter(text: str) -> list[str]:
    """Return the lines between the first two `---` markers, else []."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return lines[1:idx]
    return []


def extract_body(text: str) -> str:
    """Return everything after the closing `---` of the frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1 :])
    return ""


def collect_include_globs(frontmatter: list[str]) -> list[str]:
    """Return every glob under the trigger: block's files.include: lists.

    Indent-light state machine: `trigger:` is a top-level key whose value is
    a list of mappings with files.include / files.exclude lists. Both the
    block style (`include:` followed by `- "glob"` items) and the flow style
    (`include: ["a", "b"]`) are collected, quoted or bare. Everything else
    (exclude:, items outside trigger:, scalar keys) is ignored.
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
            flow = stripped[len("include:") :].strip()
            for item in FLOW_ITEM_RE.findall(flow):
                globs.append(item)
            continue
        if stripped.startswith("exclude:"):
            in_include = False
            continue
        if not in_include:
            continue
        match = LIST_ITEM_RE.match(line)
        if match:
            item = match.group(1).strip().strip("\"'")
            if item:
                globs.append(item)
        else:
            in_include = False
    return globs


def glob_exists(pattern: str) -> bool:
    """True when the repo-rooted glob matches at least one path."""
    return bool(glob.glob(f"{glob.escape(str(ROOT))}/{pattern}", recursive=True))


def main() -> int:
    if not SCAN_DIR.is_dir():
        print(f"skill-trigger-paths: scanned tree not found: {SCAN_DIR}", file=sys.stderr)
        return 2
    stale: list[str] = []
    total_globs = 0
    try:
        for skill_dir in sorted(p for p in SCAN_DIR.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            text = skill_md.read_text(encoding="utf-8")
            frontmatter = extract_frontmatter(text)
            globs = collect_include_globs(frontmatter)
            # Fail closed on parser drift: an include: key that yielded zero
            # parseable globs means this parser no longer understands the
            # frontmatter shape (review feedback: unquoted/flow/indent
            # variants used to reduce to a silent "STALE 0").
            if not globs and re.search(r"\binclude:", "\n".join(frontmatter)):
                print(
                    f"skill-trigger-paths: include: block in {skill_dir.name}/SKILL.md "
                    "yielded zero parseable globs (parser drift)",
                    file=sys.stderr,
                )
                return 2
            total_globs += len(globs)
            for pattern in globs:
                posix = pattern.replace("\\", "/")
                if posix.startswith("/") or re.match(r"^[A-Za-z]:", posix) or ".." in posix.split("/"):
                    stale.append(f"STALE {pattern}: {skill_dir.name} (escapes the repository root)")
                    continue
                matches = glob.glob(f"{glob.escape(str(ROOT))}/{pattern}", recursive=True)
                # A glob kept alive only by test files does not fire the
                # skill for production changes (the generated skills'
                # own exclude convention is *.test.* / *.spec.*).
                if not any(not TEST_FILE_RE.search(m) for m in matches):
                    stale.append(f"STALE {pattern}: {skill_dir.name}")
            # Body references: a trigger-only fix must not leave the
            # checklist pointing at deleted paths (PR #880 review).
            # :line and ::Symbol citation suffixes are stripped first — they
            # point at locations inside existing files and rot by design.
            for ref in BODY_PATH_RE.findall(extract_body(text)):
                path = re.split(r"::|:\d+\s*$", ref, maxsplit=1)[0]
                if not glob_exists(path):
                    stale.append(f"STALE {ref}: {skill_dir.name} (body reference)")
    except OSError as exc:
        print(f"skill-trigger-paths: {exc}", file=sys.stderr)
        return 2

    if stale:
        for line in stale:
            print(line)
        return 1
    if total_globs == 0:
        # A vacuous sweep: nothing was scanned, so "STALE 0" would be silent
        # pass-theater (review feedback, PR #880).
        print("skill-trigger-paths: zero include globs collected from any generated skill", file=sys.stderr)
        return 2
    print("STALE 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
