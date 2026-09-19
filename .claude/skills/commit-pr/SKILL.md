---
name: commit-pr
description: Commit, push, publish, ship, or open a GitHub pull request for ragappv3 changes. Use for PR creation, PR review follow-up pushes, draft PR updates, and release-ready local changes in this repo. Enforces ragappv3 branch hygiene, scoped staging, conventional commit titles, draft PRs against master, and Python/FastAPI plus npm/Vite validation. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/commit-pr/SKILL.md"
---

# commit pr (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/commit-pr/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
