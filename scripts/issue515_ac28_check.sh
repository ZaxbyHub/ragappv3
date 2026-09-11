#!/usr/bin/env bash
# issue515 combined acceptance check AC28: backend API contract + frontend feedback.
set -e
HERE="$(cd "$(dirname "$0")" && pwd -P)"
bash "$HERE/issue515_pytest.sh" tests/test_issue515_memories.py::TestAC28WhitespaceContentRejected::test_issue515_ac28_whitespace_content_rejected
bash "$HERE/issue515_vitest.sh" src/tests/issue515-memory.test.tsx -t "issue515-ac28"
