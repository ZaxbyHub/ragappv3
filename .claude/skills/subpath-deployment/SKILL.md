---
name: subpath-deployment
description: > RAGAPPv3 subpath/reverse-proxy deployment system. Load before touching anything related to APP_ROOT_PATH, VITE_APP_BASENAME, VITE_API_URL, cookie paths, SPA catch-all routing, proxy configuration, or Docker build args for the frontend. Contains locked architectural decisions that must not be re-litigated. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/subpath-deployment/SKILL.md"
---

# subpath deployment (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/subpath-deployment/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
