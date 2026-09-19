---
name: tech-debt-ci-review
description: Deep technical debt and CI stability audit for identifying test theater, missing or mis-scoped tests, actual and potential test failures, flaky-test risk, dependency/toolchain brittleness, and structural debt that prevents PRs from going green safely. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/tech-debt-ci-review/SKILL.md"
---

# tech debt ci review (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/tech-debt-ci-review/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
