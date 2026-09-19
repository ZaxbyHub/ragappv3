---
name: config-env-contract-check
description: Audit environment and configuration contracts across backend settings, frontend env usage, Docker, Compose, CI, docs, and examples. Use when changing config, deployment, root paths, CORS, settings schemas, env examples, or Docker files. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/config-env-contract-check/SKILL.md"
---

# config env contract check (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/config-env-contract-check/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
