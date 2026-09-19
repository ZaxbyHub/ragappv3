---
name: swarm
description: Enable a high-quality swarm-like workflow for the current session, and optionally execute a task immediately using that mode. Uses parallel subagents for breadth, independent reviewer validation for precision, and critic challenge for final confidence. Use when the user wants swarm-like behavior, higher review rigor, or maximum quality without sacrificing the agent runner's native speed. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/swarm/SKILL.md"
---

# swarm (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/swarm/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
