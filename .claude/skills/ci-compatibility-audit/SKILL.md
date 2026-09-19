---
name: ci-compatibility-audit
description: Lightweight PR-time audit for whether changes are compatible with the actual RAGAPPv3 GitHub Actions workflow, dependency lockfiles, scripts, and cross-platform local validation. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/ci-compatibility-audit/SKILL.md"
---

# ci compatibility audit (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/ci-compatibility-audit/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
