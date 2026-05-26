---
name: preflight
description: "Run this repo's CI gates locally before pushing or opening a PR. Mirrors .github/workflows/ci.yml exactly (backend ruff, frontend typecheck/lint/test, quality-contract scripts) so PRs go green on the first try. Triggers: preflight, pre-push check, will CI pass, run CI locally, check before push, before I open the PR."
metadata:
  {
    "openclaw":
      {
        "emoji": "🛫",
        "requires": { "bins": ["python", "ruff", "npm"] },
      },
  }
---

# Preflight

Run the **exact** checks CI runs, locally, before you push. This repo's CI
(`.github/workflows/ci.yml`) has three required jobs — **Backend**,
**Frontend**, and **Quality contracts**. A failure in any one blocks merge and
costs a full push → CI → fixup-commit round trip. Run them here first.

## Environment caveats (read before trusting results)

- **Python: CI pins 3.11.** If your local interpreter is newer (e.g. 3.14),
  some tests fail locally with `RuntimeError: There is no current event loop`
  (the test harness uses the removed implicit-event-loop pattern). Those are
  **false failures** — the lint gate and CI-targeted tests are what matter.
  Prefer a 3.11 venv if available; otherwise read pytest results with this in
  mind.
- **Frontend deps:** if `frontend/node_modules` is missing, run `npm ci` first
  (CI uses `npm ci --engine-strict` on Node 20.19.0).

## The three gates

Run from the repo root. Each block matches a CI job step-for-step.

### 1. Backend (`backend/`)

```bash
cd backend
# Lint — this is the gate that most often fails in CI (import sorting, I001).
ruff check .
# CI-targeted tests (the exact subset CI runs):
pytest --tb=short -v tests/test_path_prefix.py tests/test_auth_routes.py tests/test_main_catchall.py
```

When you've changed a specific area, **also** run that area's tests (CI's
targeted subset will not cover new modules). Example for the documents/tags
work: `pytest -q tests/test_tags_routes.py tests/test_vault_document_permissions_regression.py`.

### 2. Frontend (`frontend/`)

```bash
cd frontend
# Install only if node_modules is absent:
[ -d node_modules ] || npm ci --engine-strict
npm run typecheck   # tsc --noEmit
npm run lint        # eslint src --max-warnings 0  (zero-warning gate)
npm test            # vitest run
npm run build       # tsc && vite build
# Also verify the subpath deployment build (CI runs this separately):
VITE_APP_BASENAME=/knowledgevault VITE_API_URL=/knowledgevault/api npm run build
```

When a frontend test fails on a jsdom quirk (router context, Radix `Select`,
virtualized rows), see `references/frontend-testing-gotchas.md` for the repo's
established mock patterns before improvising.

### 3. Quality contracts (repo root)

```bash
python scripts/check_config_contract.py
python scripts/check_pr_scope_drift.py
```

## Reporting

After running, report a per-gate punch list: `PASS` / `FAIL` for Backend lint,
Backend tests, Frontend typecheck/lint/test/build, and each contract script.
Only declare "CI will pass" when every gate is green (accounting for the Python
3.11 caveat above). If a gate fails, fix the root cause and re-run that gate —
do not push hoping CI differs.
