---
name: commit-pr
description: >
  Mandatory publication protocol for the GitHub Copilot coding agent and custom
  Copilot agents in ragappv3. Load when assigned to an issue or when committing,
  pushing, opening/updating/readying a PR, or closing out remote CI. Routes to
  the single canonical commit-pr source of truth.
---

# Commit PR (GitHub Copilot adapter)

This repository has exactly **one** publication workflow, and it is mandatory.

This file is a discovery shim so the GitHub Copilot agent can find the workflow
under a `.github/skills` path. It is **not** the source of truth and
intentionally duplicates none of the contract. The canonical, authoritative
protocol is
[`../../../.claude/skills/commit-pr/SKILL.md`](../../../.claude/skills/commit-pr/SKILL.md).

Read and follow, in order:

1. [`../../../AGENTS.md`](../../../AGENTS.md) — root engineering contract
2. [`../../../docs/engineering/conventions.md`](../../../docs/engineering/conventions.md) — engineering conventions
3. [`../../../.claude/skills/commit-pr/SKILL.md`](../../../.claude/skills/commit-pr/SKILL.md) — **single source of truth**

If instructions ever conflict, precedence is: `AGENTS.md` →
`docs/engineering/conventions.md` → `.claude/skills/commit-pr/SKILL.md` → this
file.

Do not commit, push, run `gh pr create`, `gh pr edit`, or `gh pr ready`, edit a
PR body, mark a PR ready, or claim CI/merge readiness until the canonical
`commit-pr` checklist is satisfied.

The required PR title, branch hygiene, scoped staging, validation suite
(targeted `pytest`, `npm test`, `ruff check .`, `npm run typecheck`/`lint`/`build`),
draft/ready behavior (default draft against `master`), and CI closeout rules all
come from
[`../../../.claude/skills/commit-pr/SKILL.md`](../../../.claude/skills/commit-pr/SKILL.md).
The PR body must include `## Summary` and `## Test plan`, plus `## Review
follow-up` only when addressing review feedback. The `quality-contracts` CI job
and the `pr-publication-gate` hook help enforce this contract; do not work
around them.
