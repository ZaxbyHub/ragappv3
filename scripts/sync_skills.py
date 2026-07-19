#!/usr/bin/env python3
"""Propagate and check repo-specific skills across the three runner trees.

The repo carries three agent-runner skill trees (.claude/skills/,
.agents/skills/, .opencode/skills/). AGENTS.md and
docs/engineering/conventions.md mandate that repo-specific skills be mirrored
across all three. This script enforces and performs that mirroring.

Drift definition (intent-based, see docs/engineering/skill-conventions.md):
a repo-specific skill that is present in two or more trees must be present in
all three with byte-identical content. Runner-specific, framework-vendored,
adapter, and generated skills are allowlisted and exempt from the rule.

Canonical-tree precedence (.agents > .claude > .opencode):
docs/releases/pending/skills-narrowed-directives.md documents that
.opencode/skills/ is the opencode-swarm plugin's internal area and is skipped
when injecting skills for Claude Code or Codex sessions. The .agents tree is
the smallest and most repo-focused; .claude is consumed by Claude Code.
.opencode is the last-resort source.

Modes:
  default   propagate canonical copies to drift targets (idempotent)
  --check   report drift to stderr, exit 1 on drift, 0 if clean (CI mode)
  --dry-run print planned changes without writing

Exit codes: 0 = success / no drift, 1 = drift found (in --check) or error.
Run from the repository root.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TREES = (".agents/skills", ".claude/skills", ".opencode/skills")
CANONICAL_PRECEDENCE = (
    ".agents/skills",
    ".claude/skills",
    ".opencode/skills",
)
GENERATED_SUBGROUP = ".opencode/skills/generated"

# Upstream swarm-framework MODE: protocol skills vendored into .claude and
# .opencode. They reference deleted .swarm/ and absent src/agents/architect.ts,
# and are NOT in AGENTS.md's repo-specific skill list. Out of scope for the
# mirror rule; tracked separately upstream.
FRAMEWORK_VENDORED_ALLOWLIST = {
    "brainstorm",
    "clarify",
    "clarify-spec",
    "consult",
    "council",
    "critic-gate",
    "deep-dive",
    "design-docs",
    "discover",
    "execute",
    "issue-ingest",
    "phase-wrap",
    "plan",
    "pre-phase-briefing",
    "resume",
    "specify",
}

# Runner-specific single-tree skills. Vendored upstream plugins or runner-side
# helpers that the other runners do not load.
RUNNER_SPECIFIC_ALLOWLIST = {
    # .claude-only: Claude-Code-runner / upstream-plugin skills
    "agentic-engineering",
    "autonomous-loops",
    "coding-agent",
    "gh-issues",
    "github",
    "plankton-code-quality",
    "reviewing-code-core",
    "reviewing-dependencies",
    "reviewing-doc-drift",
    "reviewing-security",
    "ship",
    # .agents-only: Codex-side
    "contributing",
    "subprocess-safety",
    # .opencode-only (top-level): opencode-swarm plugin skills
    "deep-research",
    "loop",
    "swarm-pr-subscribe",
}

# Adapter skills: one tree holds the canonical protocol; the other trees hold
# thin adapters that point to it. Per AGENTS.md:30 + .opencode/skills/
# codebase-review-swarm/INSTALL.md. Adapter copies are NOT byte-identical to
# the canonical copy; the sync check verifies the adapter relationship instead.
ADAPTER_SKILLS = {
    "codebase-review-swarm",
}

# Per-skill canonical-tree override. Use when the maintainer wants to preserve
# content found only in a non-precedence tree. Documented escape hatch in
# docs/engineering/skill-conventions.md.
PER_SKILL_CANONICAL: dict[str, str] = {
    # The .opencode copy carries a useful force-push addendum absent from
    # .agents; treat .opencode as canonical for this skill so propagation
    # preserves it rather than deleting it.
    "ci-fix-monitor": ".opencode/skills",
    # .claude/commit-pr has runner-neutral branch-prefix guidance
    # ("claude/ for Claude Code, codex/ for Codex") and the canonical ragappv3
    # frontmatter (no `effort:` key); .agents still says just "codex/".
    "commit-pr": ".claude/skills",
}

ALL_ALLOWLISTS = (
    FRAMEWORK_VENDORED_ALLOWLIST
    | RUNNER_SPECIFIC_ALLOWLIST
    | ADAPTER_SKILLS
)
# NOTE: PER_SKILL_CANONICAL keys are NOT allowlisted — they are normal
# repo-specific skills with a non-default canonical tree. They still must
# be mirrored byte-identically across all trees that contain them.

# Adapter validation thresholds.
ADAPTER_POINTER_PREFIX = ".opencode/skills/"
ADAPTER_THINNESS_RATIO = 0.30


def all_skill_names() -> set[str]:
    """Return every skill dir name found at the top level of any tree.

    Skips the GENERATED_SUBGROUP container (`.opencode/skills/generated/`),
    which holds auto-generated knowledge skills rather than a top-level skill.
    The structural skip (no top-level SKILL.md in that dir today) is the
    primary mechanism; this filter is defensive in case a top-level
    SKILL.md is ever added there.
    """
    names: set[str] = set()
    for tree in TREES:
        tree_dir = ROOT / tree
        if not tree_dir.is_dir():
            continue
        for child in tree_dir.iterdir():
            if not child.is_dir():
                continue
            # Defensive: never treat the generated-subgroup container as a
            # skill even if a stray SKILL.md appears at its root.
            if (ROOT / GENERATED_SUBGROUP).resolve() == child.resolve():
                continue
            if (child / "SKILL.md").is_file():
                names.add(child.name)
    return names


def canonical_tree_for(skill: str) -> str:
    """Pick the canonical source tree for a skill, honoring per-skill overrides."""
    if skill in PER_SKILL_CANONICAL:
        return PER_SKILL_CANONICAL[skill]
    present = [t for t in CANONICAL_PRECEDENCE if (ROOT / t / skill / "SKILL.md").is_file()]
    if not present:
        raise RuntimeError(f"canonical_tree_for called on absent skill: {skill}")
    return present[0]


def _copy_skill_dir(src: Path, dst: Path) -> None:
    """Replace dst with a recursive copy of src."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _print(msg: str) -> None:
    print(msg, file=sys.stderr)


def check_drift() -> list[str]:
    """Return a list of human-readable drift findings. Empty list = clean."""
    findings: list[str] = []

    for skill in sorted(all_skill_names()):
        if skill in ALL_ALLOWLISTS and skill not in ADAPTER_SKILLS:
            continue
        present_in = [t for t in TREES if (ROOT / t / skill / "SKILL.md").is_file()]

        if len(present_in) < 2:
            # Legitimate single-tree skill (allowlisted or just unused elsewhere).
            continue

        if skill in ADAPTER_SKILLS:
            findings.extend(_check_adapter(skill, present_in))
            continue

        # Shared repo-specific skill: must be in all three trees, byte-identical.
        for tree in TREES:
            if tree not in present_in:
                findings.append(f"skill-sync: {skill} missing in {tree}")

        canonical_dir = ROOT / canonical_tree_for(skill) / skill
        for tree in present_in:
            other = ROOT / tree / skill
            if not _dirs_equal(canonical_dir, other):
                findings.append(
                    f"skill-sync: {skill} diverges between "
                    f"{canonical_dir.relative_to(ROOT)} and {other.relative_to(ROOT)}"
                )

    return findings


def _dirs_equal(a: Path, b: Path) -> bool:
    """True if dirs a and b have identical file set and file contents."""
    a_files = {p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file()}
    b_files = {p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file()}
    if a_files != b_files:
        return False
    for rel in a_files:
        if (a / rel).read_bytes() != (b / rel).read_bytes():
            return False
    return True


def _check_adapter(skill: str, present_in: list[str]) -> list[str]:
    """Validate the adapter relationship for an adapter skill."""
    findings: list[str] = []
    canonical_path = ROOT / ".opencode" / "skills" / skill / "SKILL.md"
    if not canonical_path.is_file():
        findings.append(
            f"skill-sync: adapter {skill} missing canonical copy at "
            f"{canonical_path.relative_to(ROOT)}"
        )
        return findings

    canonical_lines = sum(1 for _ in canonical_path.open(encoding="utf-8"))
    for tree in present_in:
        if tree == ".opencode/skills":
            continue
        adapter = ROOT / tree / skill / "SKILL.md"
        if not adapter.is_file():
            findings.append(f"skill-sync: adapter {skill} missing in {tree}")
            continue
        body = adapter.read_text(encoding="utf-8")
        if ADAPTER_POINTER_PREFIX + skill + "/" not in body:
            findings.append(
                f"skill-sync: adapter {skill} in {tree} does not point at "
                f"canonical {ADAPTER_POINTER_PREFIX}{skill}/"
            )
            continue
        adapter_lines = sum(1 for _ in adapter.open(encoding="utf-8"))
        if canonical_lines and adapter_lines > canonical_lines * ADAPTER_THINNESS_RATIO:
            findings.append(
                f"skill-sync: adapter {skill} in {tree} is not thin "
                f"({adapter_lines} lines vs canonical {canonical_lines}; "
                f"ratio {adapter_lines / canonical_lines:.0%} > "
                f"{ADAPTER_THINNESS_RATIO:.0%})"
            )
    return findings


def propagate(dry_run: bool = False) -> list[str]:
    """Propagate canonical copies to drift targets. Returns change log."""
    changes: list[str] = []
    for skill in sorted(all_skill_names()):
        if skill in ALL_ALLOWLISTS and skill not in ADAPTER_SKILLS:
            continue
        present_in = [t for t in TREES if (ROOT / t / skill / "SKILL.md").is_file()]
        if len(present_in) < 2 or skill in ADAPTER_SKILLS:
            continue

        canonical_tree = canonical_tree_for(skill)
        canonical_dir = ROOT / canonical_tree / skill

        for tree in TREES:
            if tree == canonical_tree:
                continue
            target = ROOT / tree / skill
            if tree not in present_in:
                msg = f"skill-sync: copy {skill} {canonical_tree} -> {tree}"
                changes.append(msg)
                if not dry_run:
                    _copy_skill_dir(canonical_dir, target)
            elif not _dirs_equal(canonical_dir, target):
                msg = (
                    f"skill-sync: update {skill} in {tree} "
                    f"from canonical {canonical_tree}"
                )
                changes.append(msg)
                if not dry_run:
                    _copy_skill_dir(canonical_dir, target)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report drift; no writes (CI mode)")
    mode.add_argument("--dry-run", action="store_true", help="print planned changes; no writes")
    args = parser.parse_args(argv)

    if args.check:
        findings = check_drift()
        for line in findings:
            _print(line)
        if findings:
            return 1
        print("skill-sync: all checks passed")
        return 0

    changes = propagate(dry_run=args.dry_run)
    if changes:
        for line in changes:
            (print if args.dry_run else _print)(line)
        verb = "would change" if args.dry_run else "changed"
        print(f"skill-sync: {verb} {len(changes)} skill(s)")
        return 0
    print("skill-sync: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
