---
name: swarm-pr-review
description: Run a graph-guided, tool-augmented Swarm PR review using context packing, parallel exploration, triggered plugin micro-lanes, independent reviewer validation, critic challenge, and metrics writeback. Use for deep pull request review with low false-positive tolerance and high recall. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/swarm-pr-review/SKILL.md"
---

# swarm pr review (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/swarm-pr-review/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
