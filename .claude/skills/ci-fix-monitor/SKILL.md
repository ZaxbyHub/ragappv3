---
name: ci-fix-monitor
description: Monitor and fix CI on an open RAGAPPv3 pull request until every required check is green. Load when asked to watch CI, diagnose red checks, or drive a PR to passing. Maps the four real CI jobs (Frontend, Backend, Quality contracts, SAST), enforces diagnose-before-fix, and covers re-push / rebase. This repo has NO dist-check, biome, bun, or per-OS matrix — ignore that guidance. Updated for PR #215 (issue #209): Backend now runs the full `pytest tests/` suite (3918 tests, ~18m on CI Linux) — NOT the old 3-file subset. Job timeout is 60m. pytest-timeout=300 caps per-test hangs. Adapter pointing to the canonical repo skill; refer to that for the full protocol.
metadata:
  adapter_for: ".agents/skills/ci-fix-monitor/SKILL.md"
---

# ci fix monitor (Claude Code adapter)

This is a thin pointer to the canonical skill at
`.agents/skills/ci-fix-monitor/SKILL.md`. Claude Code runners should load the canonical SKILL.md
there for the full protocol; this adapter exists only so Claude Code's
`.claude/skills/` discovery finds the skill.

Canonical tree for repo-specific skills: `.agents/skills/` (see
`docs/engineering/skill-conventions.md`).
