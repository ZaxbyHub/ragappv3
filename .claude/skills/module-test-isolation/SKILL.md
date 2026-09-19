---
name: module-test-isolation
description: Document the discipline of isolating module-level mutable state in tests. Use when adding tests that touch global registries, singletons, or class attributes. Prevents test pollution that surfaces only in combined pytest sessions. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/module-test-isolation/SKILL.md"
---

# module test isolation (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/module-test-isolation/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
