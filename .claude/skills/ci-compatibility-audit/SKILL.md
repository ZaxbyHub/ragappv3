---
name: ci-compatibility-audit
description: Lightweight PR-time audit for whether changes are compatible with the actual RAGAPPv3 GitHub Actions workflow, dependency lockfiles, scripts, and cross-platform local validation.
effort: medium
---

# CI Compatibility Audit

Use this skill before pushing workflow, dependency, test, build, lint, Docker, or tooling changes.

## Current CI Map

Primary workflow: `.github/workflows/ci.yml`

Frontend job:

- `cd frontend`
- `npm ci --engine-strict`
- frontend toolchain graph check with `node --version`, `npm --version`, `npm ls vite vitest @vitejs/plugin-react jsdom`, `npm exec vite -- --version`, and `npm exec vitest -- --version`
- `npm run typecheck`
- `npm run lint`
- API smoke tests for shared API, CSRF/SSE streaming, wiki SSE URL, and auth API-base behavior
- `npm test`
- `npm run build`
- subpath build with `VITE_APP_BASENAME=/knowledgevault` and `VITE_API_URL=/knowledgevault/api`

Backend job:

- `cd backend`
- `pip install -r requirements-ci.txt`
- `pip install -r requirements-dev.txt`
- `ruff check .`
- `pytest --tb=short -v`
- informational coverage

Repository contract job:

- `python scripts/check_config_contract.py`
- `python scripts/check_pr_scope_drift.py`

## Checks

- Lockfiles exist and match the package manager used by CI.
- CI commands exist in package manifests or requirements files.
- Cache paths point at real lockfiles.
- Scripts do not depend on local-only absolute paths.
- Workflow shell syntax is valid on the configured runner.
- Pull request diff checks have enough fetch depth.
- Local validation commands mirror CI when possible.
- Truncated CI output does not hide the command exit status.

## Local Mirror Commands

```bash
cd frontend && npm ci --engine-strict && npm run typecheck && npm run lint
cd frontend && npm test -- src/lib/api.test.ts src/lib/api.csrf.test.ts src/lib/api.sse.test.ts src/pages/WikiPage.sse.test.tsx src/stores/useAuthStore.api-base.test.ts
cd frontend && npm test && npm run build
cd frontend && VITE_APP_BASENAME=/knowledgevault VITE_API_URL=/knowledgevault/api npm run build
cd backend && ruff check . && pytest --tb=short -v
python scripts/check_config_contract.py
python scripts/check_pr_scope_drift.py
```

Run these before pushing so a CI-only lint/type failure doesn't cost a
push → fail → fixup-commit round trip. If `frontend/node_modules` is absent,
run `npm ci --engine-strict` first.

## Environment caveats (so local results aren't misread)

- **Python: CI pins 3.11.** On a newer local interpreter (e.g. 3.14) some
  backend tests fail with `RuntimeError: There is no current event loop` — the
  test harness uses the removed implicit-event-loop pattern. These are **false
  failures from the local interpreter, not regressions**. Prefer a 3.11 venv;
  the `ruff check .` lint gate and CI-targeted tests are what matter.
- **Frontend jsdom gotchas** (router context for `<Link>`, driving Radix
  `Select`, virtualized lists): see `references/frontend-testing-gotchas.md`
  for the repo's established mock patterns before improvising.

## Output

Classify each risk as:

- `BLOCKER`: likely CI failure or invalid workflow.
- `RISK`: plausible CI instability requiring targeted validation.
- `NOTE`: useful context, not blocking.

Include the exact workflow step or command for every item.
