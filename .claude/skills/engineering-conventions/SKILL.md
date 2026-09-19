---
name: engineering-conventions
description: > RAGAPPv3 engineering conventions — backend (FastAPI + SQLite + LanceDB), frontend (React + TypeScript + Vite + Vitest), database/migration/RBAC patterns, and the multi-agent skill layout. Load before implementing or refactoring backend routes/services/migrations or frontend pages/components, or when you need to know "how does this repo do X". Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/engineering-conventions/SKILL.md"
---

# engineering conventions (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/engineering-conventions/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
