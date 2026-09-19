---
name: issue-tracer
description: "Use when asked to trace, investigate, root-cause, plan, fix, close, or prepare a PR for a GitHub issue or bug report. Runs an evidence-first issue workflow: GitHub intake, reproduction, reasoning-guided localization, no-gap fix planning, independent critic review, user approval gate, implementation, tests, and PR-ready closure. Adapter pointing to the canonical repo skill; refer to that for the full protocol."
metadata:
  adapter_for: ".agents/skills/issue-tracer/SKILL.md"
---

# issue tracer (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/issue-tracer/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
