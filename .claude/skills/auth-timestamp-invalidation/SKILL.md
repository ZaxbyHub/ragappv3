---
name: auth-timestamp-invalidation
description: Document the discipline of propagating timestamp fields through token invalidation flows. Use when implementing access-token refresh reuse, password change epoch, or any token revocation flow that must invalidate outstanding tokens. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/auth-timestamp-invalidation/SKILL.md"
---

# auth timestamp invalidation (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/auth-timestamp-invalidation/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
