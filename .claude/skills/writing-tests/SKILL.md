---
name: writing-tests
description: RAGAPPv3 testing policy and conventions. Load before writing or modifying any test, fixing a test/CI failure, or adding coverage. Covers backend pytest + unittest (SimpleConnectionPool dependency-override harness, FK cascades, the Python 3.11-vs-local event-loop trap) and frontend Vitest + React Testing Library + jsdom (MemoryRouter, Radix Select, react-virtual mock patterns). This repo's frontend uses Vitest, NOT bun:test. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/writing-tests/SKILL.md"
---

# writing tests (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/writing-tests/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
