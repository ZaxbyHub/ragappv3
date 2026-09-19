---
name: swarm-pr-feedback
description: Ingest an external PR review bundle (from swarm-pr-review, a human reviewer, or CI), verify each finding against current code, fix confirmed findings, add regression tests, amend the commit, and push. Use when given a structured review output with classified findings to resolve on an existing PR branch. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/swarm-pr-feedback/SKILL.md"
---

# swarm pr feedback (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/swarm-pr-feedback/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
