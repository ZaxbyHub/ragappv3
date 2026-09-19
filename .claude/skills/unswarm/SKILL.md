---
name: unswarm
description: Disable swarm mode for the current session and return to normal behavior. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/unswarm/SKILL.md"
---

# unswarm (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/unswarm/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
