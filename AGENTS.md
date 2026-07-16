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

Each runner loads skills from its own tree — `.claude/skills/` (Claude Code),
`.agents/skills/` (Codex), `.opencode/skills/` (opencode-swarm). Repo-specific
skills:

- `engineering-conventions` — points to `docs/engineering/conventions.md`.
- `writing-tests` — points to `docs/engineering/testing.md`.
- `ci-compatibility-audit` — reproduce CI gates locally before pushing.
- `commit-pr` — branch / commit / PR protocol.
- `config-env-contract-check`, `review-finding-validator` — config-contract and finding-validation helpers.
- `codebase-review-swarm` — read-only, quote-grounded full-repo audit (Phase 0 inventory, selected-track depth, reviewer/critic validation); canonical at `.opencode/skills/codebase-review-swarm/`.

When you add or change a repo-specific skill, mirror it across all three trees
(or keep it a thin pointer to a canonical doc) so every runner stays consistent.

## Non-negotiables

- **CI is the source of truth.** Before any push or PR, run `ci-compatibility-audit`: backend `ruff check .` + targeted pytest, frontend `typecheck`/`lint`/`test`/`build`, and `scripts/check_*.py`. A lint/type error caught locally is free; caught in CI it costs a round trip.
- **CI pins Python 3.11.** On local 3.14+, some tests fail with `RuntimeError: There is no current event loop` — that's a local-interpreter artifact, not a regression. See `docs/engineering/testing.md`.
- **Frontend is Vitest, not `bun:test`.** Ignore any `bun:test` guidance.
- **Never ship unwired code, never defer work, and never make scope decisions without explicit instruction.**
- New behavior ships with tests; assert real behavior, not just status codes.

## CI gates

`.github/workflows/ci.yml` — jobs: **Backend** (ruff + targeted pytest),
**Frontend** (typecheck, lint `--max-warnings 0`, test, build, subpath build),
**Quality contracts** (`check_config_contract.py`, `check_pr_scope_drift.py`, `check_sast_baseline.py`),
**SAST** (`scripts/run_bandit.py` — bandit baseline gate, fails on new findings).
