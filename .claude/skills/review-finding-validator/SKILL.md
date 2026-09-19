---
name: review-finding-validator
description: Validate external reviewer, CI, audit, swarm, or PR findings as claims before implementing or reporting them. Use when given a bundle of review findings, requested-changes comments, audit output, or suspected regressions that must be classified with evidence. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/review-finding-validator/SKILL.md"
---

# review finding validator (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/review-finding-validator/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
