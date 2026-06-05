---
name: contributing
description: >
  End-to-end contribution workflow for RAGAPPv3 (KnowledgeVault). Load before
  creating branches, commits, or PRs. Covers branch hygiene against master,
  conventional commits, the three real CI gates (Frontend, Backend, Quality
  contracts), and where to go for publication. This repo is Python/FastAPI +
  npm/Vite — there is NO release-please, biome, bun, or dist-check.
---

# Contributing to RAGAPPv3 (KnowledgeVault)

The human-facing version of this process is `CONTRIBUTING.md`; the agent entry
point is `AGENTS.md`. This skill is the actionable checklist. For the actual
commit/push/PR mechanics (staging, force-with-lease, draft PRs, review
follow-up), defer to **`.claude/skills/commit-pr/SKILL.md`** — do not duplicate
it here.

## 1. Branch setup

- **Base branch is `master`.** Branch from the latest `origin/master`.
- Branch names: short, prefixed slugs, e.g. `fix/tag-cascade`, `feat/bulk-export`.
  Agent runners may use a runner prefix (`claude/…`, `codex/…`) per `commit-pr`.

```bash
git fetch origin master
git checkout -b fix/<short-slug> origin/master
```

## 2. Commit message format (Conventional Commits)

`type(scope): lowercase description` — no trailing period.

Allowed `type` values in this repo (per `CONTRIBUTING.md`):

| Type | Use for |
|------|---------|
| `feat` | new user-visible behavior |
| `fix` | bug fix |
| `test` | tests only |
| `docs` | documentation only |
| `refactor` | non-behavioral restructuring |
| `chore` | tooling / housekeeping |

Examples:
- `fix(tags): cascade-delete tag rows when a vault is removed`
- `feat(export): add bulk vault export endpoint`
- `test(auth): cover refresh-token rotation negative paths`

Stage explicit paths. Do not commit IDE files, local settings, `.env`, or
generated artifacts.

> There is **no** conventional-commit CI linter and **no** PR-title check job in
> this repo — but `master` history and the human reviewers expect the format, so
> follow it.

## 3. Run the CI gates locally (three jobs, all must pass)

CI (`.github/workflows/ci.yml`) is PR-triggered against `master` and has exactly
three jobs. Reproduce them so your PR goes green on the first try.

### Backend (from `backend/`)

```bash
ruff check .
# CI runs only this targeted subset:
pytest --tb=short -v tests/test_path_prefix.py tests/test_auth_routes.py tests/test_main_catchall.py
# ...plus the tests for the area you changed, e.g.:
pytest -q tests/test_tags_routes.py
```

> **Python 3.11.** CI pins 3.11. On a newer local interpreter some tests fail
> with `RuntimeError: There is no current event loop` — a local-interpreter
> artifact, not a regression. Use a 3.11 venv. (See `running-tests` /
> `writing-tests`.)

### Frontend (from `frontend/`)

```bash
npm run typecheck
npm run lint        # zero-warning gate: eslint src --max-warnings 0
npm test            # vitest run
npm run build       # tsc && vite build (CI also runs two subpath builds)
```

### Quality contracts (from repo root)

```bash
python scripts/check_config_contract.py
python scripts/check_pr_scope_drift.py
```

`check_pr_scope_drift.py` flags drift such as auth tests changed without matching
auth runtime/doc changes, and CI/tooling changes without a contract update — fix
the underlying drift rather than gaming the diff.

For the authoritative, always-current CI-mirror command list, run the
`ci-compatibility-audit` skill before pushing.

## 4. Testing expectations

New behavior ships with tests that assert **real behavior**:
- Backend: status **and** response body **and** DB state change.
- Frontend: callback args / DOM, not just "it rendered".

Cover negative paths (403/422, cross-vault isolation, cascade deletes).
Security-sensitive code gets `*_adversarial` companion tests. See `writing-tests`
and `docs/engineering/testing.md`.

## 5. Open the PR

- **Base branch: `master`.** Default to a **draft** PR unless the user asks for
  ready-for-review.
- PR body includes `## Summary` (1–3 bullets: what + why) and `## Test plan`
  (a checklist of commands run and their results).
- Never `git push --force` to a shared branch; use `--force-with-lease` to
  rewrite your own PR branch.

For the full publication flow — scoped staging, validation per change type,
review-finding handling, push safety, and PR body template — **load
`.claude/skills/commit-pr/SKILL.md`** and follow it.

## What this repo does NOT have

Do not invent or look for these — they belong to other repos:

- ❌ release-please, `CHANGELOG.md` automation, `.release-please-manifest.json`
- ❌ `docs/releases/pending/` fragments or any mandatory release-note file
- ❌ biome (lint is **eslint**; format is part of lint)
- ❌ bun / `bun:test` (backend is pytest; frontend is Vitest)
- ❌ `dist-check` / committed build output
- ❌ a per-OS test matrix (CI is Ubuntu-only)
- ❌ a PR-title / conventional-commit CI linter
- ❌ `.swarm/` evidence artifacts

## Checklist

- [ ] Branch created from latest `origin/master` with a prefixed slug
- [ ] Commits follow `type(scope): lowercase description`
- [ ] Backend: `ruff check .` + CI subset + changed-area tests pass (from `backend/`)
- [ ] Frontend: `typecheck`, `lint`, `test`, `build` pass (from `frontend/`)
- [ ] Quality contracts: both `scripts/check_*.py` pass (from repo root)
- [ ] New/updated tests assert real behavior and cover negative paths
- [ ] PR targets `master`, is a draft by default, has `## Summary` + `## Test plan`
- [ ] Publication done via `commit-pr`
