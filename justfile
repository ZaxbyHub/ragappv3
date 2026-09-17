# ragappv3 — local mirror of the CI gates (.github/workflows/ci.yml).
# Issue #567 / E10: contributors run the same commands CI runs, under one
# name, instead of reconstructing them from the workflow YAML.
#
# Prerequisites: Python 3.11, uv (lock compiler), Node >= 22.14, and just.
# The devcontainer (.devcontainer/devcontainer.json) ships a known-good
# environment with all of them.
#
# `just ci` runs every step the CI workflow runs (backend + frontend +
# quality contracts + SAST). Individual recipes run one gate at a time.

set shell := ["bash", "-cu"]

# Everything CI runs, in workflow order.
ci: backend-verify-locks \
    backend-lint \
    backend-test \
    frontend-typecheck \
    frontend-typecheck-contracts \
    frontend-lint \
    frontend-test-api \
    frontend-test-a11y \
    frontend-test \
    frontend-coverage \
    frontend-build \
    frontend-build-subpath \
    frontend-build-subpath-derived \
    quality-contracts \
    sast

# Verify both lockfiles regenerate byte-identically with the universal
# procedure (same step as the CI Backend job).
backend-verify-locks:
    #!/usr/bin/env bash
    set -euo pipefail
    for spec in requirements.txt:requirements-lock.txt requirements-ci.txt:requirements-lock-ci.txt; do
      src="backend/${spec%%:*}"
      lock="backend/${spec##*:}"
      tmp="$(mktemp)"
      cp "$lock" "$tmp"
      uv pip compile --universal --generate-hashes --no-strip-extras --no-header \
        --python-version 3.11 "$src" -o "$tmp"
      git diff --no-index --exit-code "$lock" "$tmp"
      rm -f "$tmp"
    done

# Backend lint (CI: ruff check .)
backend-lint:
    cd backend && ruff check .

# Backend tests (CI invocation; REDIS_URL empty like the workflow env)
backend-test:
    cd backend && REDIS_URL="" python -m pytest --tb=short -v --timeout=300 -rs -n auto --cov --cov-report=term-missing tests/

# Frontend type checks (CI: npm run typecheck / typecheck:contracts)
frontend-typecheck:
    cd frontend && npm run typecheck

frontend-typecheck-contracts:
    cd frontend && npm run typecheck:contracts

# Frontend lint (CI: npm run lint, --max-warnings 0)
frontend-lint:
    cd frontend && npm run lint

# Frontend API smoke tests (CI 'API smoke tests' step)
frontend-test-api:
    cd frontend && npm test -- src/lib/api.test.ts src/lib/api.csrf.test.ts src/lib/api.sse.test.ts src/pages/WikiPage.sse.test.tsx src/stores/useAuthStore.api-base.test.ts

# Frontend accessibility smoke tests (CI: npm run test:a11y)
frontend-test-a11y:
    cd frontend && npm run test:a11y

# Frontend full test suite (CI: npm test)
frontend-test:
    cd frontend && npm test

# Frontend coverage gate (CI: npm run test:coverage)
frontend-coverage:
    cd frontend && npm run test:coverage

# Frontend build (CI: npm run build)
frontend-build:
    cd frontend && npm run build

# Subpath builds (CI 'Build subpath deployment' steps). MSYS_NO_PATHCONV=1
# neutralizes Git Bash's path rewriting of the leading-slash env values
# (issue #567); it is inert on Linux and in the devcontainer.
frontend-build-subpath:
    cd frontend && MSYS_NO_PATHCONV=1 VITE_APP_BASENAME=/knowledgevault VITE_API_URL=/knowledgevault/api npm run build

frontend-build-subpath-derived:
    cd frontend && MSYS_NO_PATHCONV=1 VITE_APP_BASENAME=/meridian npm run build

# Quality contracts job (all seven scripts, CI order)
quality-contracts:
    python scripts/check_runtime_contract.py
    python scripts/check_config_contract.py
    python scripts/check_pr_scope_drift.py
    python scripts/check_sast_baseline.py
    python scripts/check_skill_sync.py
    python scripts/check_secretscan.py
    python scripts/check_test_collection_scope.py

# SAST job (CI installs backend/requirements-dev.txt first)
sast:
    python scripts/run_bandit.py
