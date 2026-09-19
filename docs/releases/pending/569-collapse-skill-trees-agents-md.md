# Skills collapsed to one canonical tree per skill; AGENTS.md is the shared instructions file

Issue: #569 (Workstream K, PR 7 of 7 — audit finding E11) · Date: 2026-09-19

## Outcome

- **One canonical home per skill.** Repo-specific skills (22) are canonical in
  `.agents/skills/`; framework-vendored skills (16) are canonical in
  `.claude/skills/`; runner-specific skills are unchanged; `codebase-review-swarm`
  keeps its documented canonical in `.opencode/skills/`; the 12 generated
  knowledge skills stay in `.opencode/skills/generated/`.
- **38 mirror copies deleted** from `.opencode/skills/` (22 repo-specific + 16
  framework-vendored). The `.opencode` tree went from 43 top-level entries to 5
  (3 plugin skills + `codebase-review-swarm` + `generated/`; the enforced
  inventory is the survivor set in `backend/tests/test_skill_tree_collapse.py`). opencode discovers
  `.claude/skills/` and `.agents/skills/` natively, so no runner loses
  discoverability.
- **22 `.claude` full copies became thin pointer adapters** (under 30 lines,
  canonical description preserved, body names the canonical
  `.agents/skills/<name>/SKILL.md`) — the same adapter pattern
  `codebase-review-swarm` already used.
- **Mirror machinery removed.** `scripts/sync_skills.py`,
  `scripts/check_skill_sync.py`, and the CI "Skill tree sync drift check" step
  are gone; the justfile `quality-contracts` recipe drops the same line. The
  collapsed model is pinned by `backend/tests/test_skill_tree_collapse.py`
  (canonical homes, valid pointers, no mirror re-introduction), which fails on
  the pre-change tree and passes after.
- **`CLAUDE.md` imports `AGENTS.md`** (`@AGENTS.md`) instead of duplicating the
  repository environment notes; Codex reads `AGENTS.md` natively.
- **`phase-wrap` content preserved**: its 2 `.opencode`-only gotcha lines were
  merged into the surviving `.claude` canonical at their positional
  counterparts before the `.opencode` copy was deleted. No other skill content
  changed (all mirrored copies were byte-identical at collapse time).
- **Scaffold deleted**: `redesign/` (31 files, self-documented as unwired since
  2026-07) removed. `specs/` split explicitly: `specs/draft-room/` (the live,
  code-referenced Draft Room invariant spec) is kept; the 15 externally
  unreferenced spec subdirectories were removed. This split removes historical
  orphans only — the `specs/<feature-name>/SPEC.md` authoring convention (the
  `ship` skill's path, following the `/spec` default) remains the supported way
  to add new specs; future spec directories are referenced by their feature's
  code and are unaffected by this cleanup.
- Canonical homes, discovery coverage per runner, size budgets, and the
  pruning tally are documented in `docs/engineering/skill-conventions.md`.

## Migration / compatibility

- Contributors: drop `python scripts/check_skill_sync.py` from any local
  pre-push ritual (the `just ci` quality-contracts recipe no longer runs it).
- Skill authors: create/edit repo-specific skills in `.agents/skills/` and add
  a thin `.claude` pointer — do not re-introduce full copies. The backend test
  above fails CI if a mirror returns.
- Historical documents (`CHANGELOG.md`, earlier release notes, audit reports)
  still describe the three-tree mirror; they are dated records and were
  intentionally not rewritten.

## Known limitations

- `swarm-pr-review` (888 lines) and `issue-tracer` (393 lines) exceed the
  agent-skills size-budget guidance; they are flagged in
  `docs/engineering/skill-conventions.md` as known debt rather than rewritten
  in this change.
- The opencode "natively discovers `.claude/skills/` and `.agents/skills/`"
  claim is grounded in the audit's runner-docs research and the
  `codebase-review-swarm` portable-install note, not a locally executable
  loader probe; the automated guard is structural (canonical homes + valid
  pointers).

## Rollback

Plain `git revert` of the collapse/machinery/scaffold commits. Deleted skill
content and specs are recoverable from git history.
