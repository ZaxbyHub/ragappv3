#!/usr/bin/env bash
# issue515 combined acceptance check AC45: backend unified-search endpoint + frontend surface.
set -e
HERE="$(cd "$(dirname "$0")" && pwd -P)"
bash "$HERE/issue515_pytest.sh" tests/test_issue515_unified_search.py::TestAC45UnifiedSearchEndpoint::test_issue515_ac45_unified_search_endpoint
bash "$HERE/issue515_vitest.sh" src/tests/issue515-memory.test.tsx -t "issue515-ac45"
