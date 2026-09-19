---
name: test-isolation-patterns
description: Document pytest pollution patterns observed in this repo where tests pass in isolation but fail in combined sessions due to shared global state. Recommend autouse fixture scoping or explicit teardown patterns to make tests reliable in any combination. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/test-isolation-patterns/SKILL.md"
---

# test isolation patterns (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/test-isolation-patterns/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
