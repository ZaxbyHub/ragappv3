#!/usr/bin/env bash
set -euo pipefail

# Verify Python 3.11 is available
if ! command -v python3.11 &> /dev/null; then
    echo "ERROR: python3.11 is not installed or not in PATH." >&2
    echo "CI targets Python 3.11. Install Python 3.11 and ensure 'python3.11' is accessible." >&2
    exit 1
fi

# Install pip-tools if pip-compile is not available
if ! command -v pip-compile &> /dev/null; then
    python3.11 -m pip install --upgrade pip-tools
fi

# Generate production lockfile
python3.11 -m piptools compile backend/requirements.txt --output-file backend/requirements-lock.txt --generate-hashes --no-header --allow-unsafe --verbose

# Generate CI lockfile
python3.11 -m piptools compile backend/requirements-ci.txt --output-file backend/requirements-lock-ci.txt --generate-hashes --no-header --allow-unsafe --verbose
