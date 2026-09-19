---
name: running-tests
description: RAGAPPv3 test EXECUTION patterns. Load when you need to run, scope, or diagnose tests — backend pytest and frontend Vitest — not when authoring them (see writing-tests for that). Covers per-file/-test targeting, CI-equivalent local runs, reading CI failure logs, output truncation recovery, and the Python 3.11 event-loop trap. This repo's frontend uses Vitest, NOT bun:test. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/running-tests/SKILL.md"
---

# running tests (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/running-tests/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
