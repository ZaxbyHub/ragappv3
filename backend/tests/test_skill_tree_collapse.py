"""Collapsed skill-tree invariants (issue #569).

The repo keeps ONE canonical home per skill: repo-specific skills are
canonical in `.agents/skills/` (thin `.claude` pointer adapters keep them
discoverable by Claude Code), framework-vendored skills are canonical in
`.claude/skills/`, and `.opencode/skills/` holds only runner-specific
opencode-swarm plugin skills, the generated knowledge subgroup, and the
codebase-review-swarm adapter canonical. The former three-tree byte-mirror
(`scripts/sync_skills.py` + `scripts/check_skill_sync.py` + the CI drift
gate) was removed; this test pins the collapsed model so a future
mirror-reintroduction or pointer rot fails in CI instead of passing
silently.

Canonical-home documentation: `docs/engineering/skill-conventions.md`
("Canonical homes").
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

REPO_MIRROR = [
    "auth-timestamp-invalidation",
    "authz-bridging-exceptions",
    "ci-compatibility-audit",
    "ci-fix-monitor",
    "commit-pr",
    "config-env-contract-check",
    "engineering-conventions",
    "issue-tracer",
    "module-test-isolation",
    "qa-sweep",
    "research-first",
    "review-finding-validator",
    "running-tests",
    "subpath-deployment",
    "swarm",
    "swarm-implement",
    "swarm-pr-feedback",
    "swarm-pr-review",
    "tech-debt-ci-review",
    "test-isolation-patterns",
    "unswarm",
    "writing-tests",
]

FRAMEWORK_VENDORED = [
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
]

RUNNER_SPECIFIC = {
    ".claude/skills": [
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
    ],
    ".agents/skills": ["contributing", "subprocess-safety"],
    ".opencode/skills": ["deep-research", "loop", "swarm-pr-subscribe"],
}

OPENCODE_SURVIVORS = {
    "deep-research",
    "loop",
    "swarm-pr-subscribe",
    "codebase-review-swarm",
    "generated",
}

GENERATED_SKILLS = {
    "council-advisory-triage",
    "dependency-ci-contract",
    "e2e-regression-async-generators",
    "fastapi-rate-limiting-integration",
    "hidden-coupling-cochange",
    "hidden-coupling-vault-auth",
    "post-removal-sweep",
    "pr-review-database-api",
    "python-async-sqlite",
    "qa-gate-disciplined-completion",
    "scalability-sqlite-pool",
    "testing-mock-async-hygiene",
}


def _skillmd(tree: str, name: str) -> Path:
    return REPO / tree / "skills" / name / "SKILL.md"


def test_repo_specific_canonical_homes() -> None:
    for name in REPO_MIRROR:
        canonical = _skillmd(".agents", name)
        assert canonical.is_file(), f"canonical missing: {canonical}"
        assert not (REPO / ".opencode" / "skills" / name).exists(), (
            f".opencode mirror must not exist for repo-specific skill {name}"
        )


def test_framework_vendored_canonical_homes() -> None:
    for name in FRAMEWORK_VENDORED:
        canonical = _skillmd(".claude", name)
        assert canonical.is_file(), f"canonical missing: {canonical}"
        assert not (REPO / ".opencode" / "skills" / name).exists(), (
            f".opencode mirror must not exist for framework-vendored skill {name}"
        )
    merged = _skillmd(".claude", "phase-wrap").read_text(encoding="utf-8")
    assert "is scanned by gates for verdict keywords" in merged, (
        "phase-wrap drift-evidence gotcha must be merged into the .claude canonical"
    )
    assert "normalizes CONCERNS verdicts" in merged, (
        "phase-wrap final-council gotcha must be merged into the .claude canonical"
    )


def test_claude_pointer_adapters_resolve() -> None:
    for name in REPO_MIRROR:
        pointer = _skillmd(".claude", name)
        assert pointer.is_file(), f"Claude Code pointer missing: {pointer}"
        lines = pointer.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 30, f"pointer must stay thin: {pointer} ({len(lines)} lines)"
        target_rel = f".agents/skills/{name}/SKILL.md"
        assert target_rel in pointer.read_text(encoding="utf-8"), (
            f"pointer body must name {target_rel}: {pointer}"
        )
        assert (REPO / target_rel).is_file(), f"pointer target missing: {target_rel}"


ADAPTER_NOTICE = (
    "Adapter pointing to the canonical repo skill; refer to that for the full protocol."
)


def _normalized_description(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    end = text.index("\n---", 3)
    description: str | None = None
    folded = False
    for line in text[3:end].split("\n"):
        if description is None:
            match = line.startswith("description:")
            if not match:
                continue
            value = line[len("description:") :].strip()
            folded = value in (">", ">-", "|", "|-")
            description = "" if folded else value
        elif line.startswith((" ", "\t")):
            description += " " + line.strip()
        elif description is not None and not folded:
            break
    assert description is not None, f"no description frontmatter: {path}"
    if len(description) >= 2 and description.startswith('"') and description.endswith('"'):
        description = description[1:-1]
    return " ".join(description.split())


def test_pointer_descriptions_track_canonicals() -> None:
    for name in REPO_MIRROR:
        canonical = _normalized_description(_skillmd(".agents", name))
        expected = canonical if canonical.endswith(".") else canonical + "."
        expected += " " + ADAPTER_NOTICE
        pointer = _normalized_description(_skillmd(".claude", name))
        assert pointer == expected, (
            f"pointer description drifted from canonical for {name}:\n"
            f"  canonical: {expected}\n  pointer:   {pointer}"
        )


def test_runner_specific_skills_stay_in_their_tree() -> None:
    for tree, names in RUNNER_SPECIFIC.items():
        for name in names:
            path = REPO / tree / name / "SKILL.md"
            assert path.is_file(), f"runner-specific skill missing: {path}"


def test_opencode_tree_holds_only_survivors() -> None:
    opencode = REPO / ".opencode" / "skills"
    top_level = {p.name for p in opencode.iterdir() if p.is_dir()}
    unexpected = sorted(top_level - OPENCODE_SURVIVORS)
    assert not unexpected, f"unexpected .opencode/skills entries: {unexpected}"
    adapter = opencode / "codebase-review-swarm"
    assert (adapter / "SKILL.md").is_file()
    assert (adapter / "README.md").is_file(), (
        "secretscan positive sample .opencode/skills/codebase-review-swarm/README.md must survive"
    )
    for tree in (".agents", ".claude"):
        adapter_path = REPO / tree / "skills" / "codebase-review-swarm" / "SKILL.md"
        body = adapter_path.read_text(encoding="utf-8")
        assert ".opencode/skills/codebase-review-swarm/" in body, (
            f"{tree} adapter must point at the canonical"
        )
        assert len(body.splitlines()) <= 30, (
            f"{tree} codebase-review-swarm adapter must stay thin "
            f"({len(body.splitlines())} lines)"
        )


def test_generated_subgroup_survives() -> None:
    generated = REPO / ".opencode" / "skills" / "generated"
    skills = {p.name for p in generated.iterdir() if p.is_dir()}
    assert skills == GENERATED_SKILLS, (
        "generated knowledge skills changed; update GENERATED_SKILLS together with "
        f"docs/engineering/skill-conventions.md. diff: {skills ^ GENERATED_SKILLS}"
    )
