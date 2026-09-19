---
name: swarm-implement
description: "Execute complex implementation work with a swarm-like workflow: parallel exploration, scoped planning, selective deep validation, and independent reviewer/critic checks where risk justifies them. Use for feature work, bug fixes, refactors, and multi-file changes. Adapter pointing to the canonical repo skill; refer to that for the full protocol."
metadata:
  adapter_for: ".agents/skills/swarm-implement/SKILL.md"
---

# swarm implement (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/swarm-implement/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
