---
name: qa-sweep
description: Apply when implementing features, fixing bugs, debugging errors, investigating failures, tracing root causes, reviewing tech debt, tracing issues, planning fixes, or completing any task. Enforces parallel sub-agent implementation, independent adversarial review, and a 95% confidence gate before stopping. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/qa-sweep/SKILL.md"
---

# qa sweep (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/qa-sweep/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
