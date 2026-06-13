#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
pip-compile requirements.txt -o requirements.lock --no-header --strip-extras --allow-unsafe
pip-compile requirements-ci.txt -o requirements-ci.lock --no-header --strip-extras --allow-unsafe
echo "Lock files regenerated. Review the diff before committing."
