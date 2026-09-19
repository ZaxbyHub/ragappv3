---
name: authz-bridging-exceptions
description: "Document two anti-patterns in the auth override branch: (a) masking non-401 HTTPExceptions when falling back to an alternative auth mechanism, and (b) using inspect.isawaitable() instead of inspect.iscoroutine() on the evaluate closure, which yields None for async def overrides. Critical for auth dependencies with JWT + service-account (SA) fallback (get_chat_stream_auth_context, get_wiki_events_auth_context, and any override branch calling get_evaluate_policy). Adapter pointing to the canonical repo skill; refer to that for the full protocol."
metadata:
  adapter_for: ".agents/skills/authz-bridging-exceptions/SKILL.md"
---

# authz bridging exceptions (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/authz-bridging-exceptions/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
