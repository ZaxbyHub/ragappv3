#!/usr/bin/env bash
# issue515 acceptance-check runner (frontend vitest). Trace infra, not product code.
# Usage: bash scripts/issue515_vitest.sh src/tests/issue515-foo.test.tsx [-t namepattern]
# In a throwaway repro worktree, node_modules is absent; link the main checkout's
# frontend/node_modules so vite/vitest resolve from the same dependency tree.
set -e
cd "$(dirname "$0")/../frontend"
if [ ! -e node_modules ] && [ ! -e ../node_modules ]; then
  common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [ -n "$common" ]; then
    main_root="$(cd "$common/.." && pwd -P)"
    if [ -e "$main_root/frontend/node_modules" ]; then
      cmd //c mklink //J "$(cygpath -w "$(pwd -P)")\\node_modules" \
        "$(cygpath -w "$main_root/frontend/node_modules")" >/dev/null 2>&1 || true
    fi
  fi
fi
exec npx vitest run "$@" 2>&1
