#!/usr/bin/env bash
# issue515 acceptance-check runner (backend pytest). Trace infra, not product code.
# Usage: bash scripts/issue515_pytest.sh tests/test_x.py::test_y [extra pytest args...]
set -e
cd "$(dirname "$0")/../backend"
exec python -m pytest "$@" -q -rf --no-header -p no:cacheprovider
