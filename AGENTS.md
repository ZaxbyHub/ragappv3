# AGENTS.md — RAGAPPv3

Entry point for AI coding agents (Codex, Claude Code, opencode-swarm) working in
this repository. Read this first, then the linked docs as needed.

## What this is

A RAG knowledge-management app:

- **Backend** — Python 3.11, FastAPI + SQLite + LanceDB, under `backend/`.
- **Frontend** — React + TypeScript + Vite, Vitest, shadcn/ui + Tailwind, under `frontend/`.

## Read these before non-trivial work

- **`docs/engineering/conventions.md`** — backend, frontend, and repo conventions (authoritative).
- **`docs/engineering/testing.md`** — testing policy and the jsdom/event-loop gotchas.
- **`CLAUDE.md`** — behavioral guidelines (applies to all agents, not just Claude).

## Skills

Skills live in one canonical tree per skill: repo-specific skills are canonical
in `.agents/skills/` (with thin `.claude` pointer adapters so Claude Code
discovers them), framework-vendored skills are canonical in `.claude/skills/`,
and `.opencode/skills/` holds only the opencode-swarm plugin's own skills plus
the generated knowledge subgroup. Repo-specific skills:

- `engineering-conventions` — points to `docs/engineering/conventions.md`.
- `writing-tests` — points to `docs/engineering/testing.md`.
- `ci-compatibility-audit` — reproduce CI gates locally before pushing.
- `commit-pr` — branch / commit / PR protocol.
- `config-env-contract-check`, `review-finding-validator` — config-contract and finding-validation helpers.
- `codebase-review-swarm` — read-only, quote-grounded full-repo audit (Phase 0 inventory, selected-track depth, reviewer/critic validation); canonical at `.opencode/skills/codebase-review-swarm/`.

When you add or change a repo-specific skill, create it in `.agents/skills/` and
add a thin pointer SKILL.md under `.claude/skills/` (the adapter pattern
`codebase-review-swarm` uses); never keep a second full copy in another tree.
Skill file structure, frontmatter shape, the canonical-homes table, the
thin-pointer adapter pattern, and the `.secretscanignore` validation contract
are specified in `docs/engineering/skill-conventions.md`. Canonical-home and
pointer integrity is enforced by `backend/tests/test_skill_tree_collapse.py`;
`.secretscanignore` validity is enforced by `scripts/check_secretscan.py`.

Instructions sharing: Codex reads this `AGENTS.md` natively; Claude Code reaches
it through the `@AGENTS.md` import in `CLAUDE.md`.

`.opencode/skill-routing.yaml` is intentionally absent: the opencode-swarm
plugin uses directory-based skill discovery (`.opencode/skills/<name>/SKILL.md`),
there is no consumer for a routing YAML, and adding one would be unwired
(see `docs/releases/pending/skills-narrowed-directives.md`).

## Non-negotiables

- **CI is the source of truth.** Before any push or PR, run `ci-compatibility-audit`: backend `ruff check .` + targeted pytest, frontend `typecheck`/`lint`/`test`/`build`, and `scripts/check_*.py`. A lint/type error caught locally is free; caught in CI it costs a round trip.
- **CI pins Python 3.11.** On local 3.14+, some tests fail with `RuntimeError: There is no current event loop` — that's a local-interpreter artifact, not a regression. See `docs/engineering/testing.md`.
- **Frontend is Vitest, not `bun:test`.** Ignore any `bun:test` guidance.
- **Never ship unwired code, never defer work, and never make scope decisions without explicit instruction.**
- New behavior ships with tests; assert real behavior, not just status codes.

## CI gates

`.github/workflows/ci.yml` — jobs: **Backend** (ruff + targeted pytest),
**Frontend** (typecheck, lint `--max-warnings 0`, test, build, subpath build),
**Playwright e2e** (`frontend/e2e/` smoke suite against the production build + stub backend; separate job, issue #573),
**Quality contracts** (`check_runtime_contract.py`, `check_config_contract.py`, `check_pr_scope_drift.py`, `check_sast_baseline.py`, `check_secretscan.py`, `check_test_collection_scope.py`),
**Detect docker scope** + **Docker build smoke** (the smoke builds the images when the docker surface changed),
**SAST** (`scripts/run_bandit.py` — bandit baseline gate, fails on new findings or unused `# nosec` suppressions).
**Closure evidence** (`.github/workflows/closure-evidence.yml` — PRs whose bodies `Closes #N` an issue must name verifiable closure evidence; `high`/`critical` issues need a cross-family approval; warn-mode rollout — see `docs/ci/closure-evidence-gate.md`).
Scheduled: `nightly.yml` (full deps) and `nightly-quality-gates.yml` (mutmut / schemathesis / stryker).
