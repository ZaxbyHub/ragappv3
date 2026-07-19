---
name: commit-pr
description: >
  Commit, push, publish, ship, or open a GitHub pull request for ragappv3 changes.
  Use for PR creation, PR review follow-up pushes, draft PR updates, and release-ready
  local changes in this repo. Enforces ragappv3 branch hygiene, scoped staging,
  conventional commit titles, draft PRs against master, and Python/FastAPI plus
  npm/Vite validation.
---

# ragappv3 Commit and PR Protocol

Use this skill when the user asks to commit, push, publish, ship, open a PR,
update a PR, or apply PR review follow-up in this repository.

## Repository Facts

- Default branch: `master`.
- Branch prefix: match your agent runner's convention — `claude/` for Claude
  Code, `codex/` for Codex — unless the user requests another prefix.
- Backend: Python/FastAPI under `backend/`.
- Frontend: npm/Vite/Vitest under `frontend/`.
- Default PR state: draft, unless the user explicitly asks for ready review.
- Keep `.Codex/`, IDE files, local settings, and generated session artifacts out of commits.

Do not use opencode-swarm or Bun publish rules in this repo. Do not require
release-please files, `.swarm` evidence cleanup, Bun tier suites, `dist/` drift
checks, or an opencode-swarm invariant audit unless those files actually exist
and the current change touches them.

## Workflow

1. Inspect state before changing git history.
   - Run `git status -sb`, `git branch --show-current`, and `git remote -v`.
   - Run `gh auth status` when a PR or push is requested.
   - Fetch the target branch with `git fetch origin master`.

2. Choose the branch strategy.
   - If detached with existing work or commits, create `<prefix>/<short-slug>` at the current `HEAD`.
   - If starting fresh from `master`, create `<prefix>/<short-slug>` from current `origin/master`.
   - If already on a feature branch for this work, stay there.
   - If the branch has diverged from `origin/master`, check mergeability before pushing.

3. Confirm scope from the diff.
   - Use `git diff --stat` and targeted `git diff -- <paths>`.
   - Stage explicit paths only. Do not use `git add -A` unless the whole worktree is known to belong to the task.
   - Leave `.Codex/` untracked unless the user explicitly asks to commit session files.

4. Validate according to the change.
   - Always run `git diff --check`; CRLF normalization warnings are non-blocking.
   - For backend code, run targeted `python -m pytest ...` and `python -m py_compile ...` for touched Python files.
   - For broader backend changes, run the relevant backend pytest module set from `backend/`.
   - For frontend code, run targeted `npm test -- <test files>` from `frontend/`.
   - Before PR publish or after review fixes touching frontend code, run `npm run typecheck` and `npm run lint -- --max-warnings 0` from `frontend/`.
   - Run `npm run build` when the change touches build configuration, routing, app shell behavior, or release-critical frontend paths.
   - If a validation command fails because dependencies are missing, install the project dependencies in the correct subdirectory and rerun once.

5. Handle external review findings.
   - Use this protocol whenever the user provides external-agent, reviewer, CI, or audit findings for an existing PR.
   - Load `review-finding-validator` for finding bundles before deciding what is true.
   - Load `config-env-contract-check` when findings or changes touch env vars, Docker, Compose, settings, CORS, root paths, or deployment docs.
   - Load `ci-compatibility-audit` when findings or changes touch workflows, package manifests, requirements, test commands, lint, build, or dependency installation.
   - Refresh current state before judging findings: fetch the target branch, inspect the current branch or PR head, and compare against the current diff.
   - Treat every finding as a claim until verified against source, diff, config, or focused runtime/test evidence.
   - Classify each finding as `confirmed`, `partially valid`, `not reproduced`, `pre-existing`, or `out of scope`, and keep brief evidence for the classification.
   - Fix confirmed findings. Fix partially valid findings only when the valid portion is in PR scope or explicitly accepted by the user/source agent.
   - Do not broaden the PR with adjacent cleanup unless required to resolve a confirmed finding.
   - Rerun focused validation for touched behavior before publishing the follow-up.
   - In the PR body, summarize `Accepted`, `Rejected`, and `Deferred/out of scope` findings with short reasons and validation evidence. Do not write exhaustive prose for low-quality rejected findings.

6. Commit cleanly.
   - Use one conventional commit: `<type>(<scope>): <lowercase description>`.
   - Prefer `feat`, `fix`, `test`, `docs`, `refactor`, or `chore`.
   - For PR review follow-up, follow `Handle external review findings`; amend the single existing commit when practical.
   - Keep the PR branch to one meaningful commit before asking for merge.

7. Push safely.
   - New branch: `git push -u origin <branch>`.
   - Amended review follow-up: `git push --force-with-lease -u origin <branch>`.
   - Never use plain `--force`.

8. Open or update the PR.
   - Base branch: `master`.
   - Default to a draft PR.
   - Title must match the conventional commit message when practical.
   - Body must include:
     - `## Summary`: 1-3 bullets explaining what changed and why.
     - `## Test plan`: markdown checklist of commands run and results.
     - `## Review follow-up`: only when addressing review feedback.
     - Known warnings or intentionally skipped checks, with reasons.

9. Final status check.
   - Run `gh pr view <number> --json url,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,commits`.
   - Run `git status -sb`.
   - Confirm only expected untracked local files remain.

## PR Body Template

```md
## Summary
- <summary bullet>

## Test plan
- [x] `<command>` -- <result>

## Review follow-up
- <review follow-up bullet>
```

Omit `## Review follow-up` when there was no review feedback.
