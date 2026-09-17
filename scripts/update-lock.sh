#!/usr/bin/env bash
set -euo pipefail

# Regenerate both backend lockfiles as UNIVERSAL locks (issue #567 / E10):
# one lock per source spec that resolves on Linux and Windows. Seeding the
# output file with the committed lock keeps every pinned version; only
# markers and platform-conditional additions change. See
# docs/engineering/lockfiles.md for the full procedure.

# Install uv if it is not available (the lock compiler since #567)
if ! command -v uv &> /dev/null; then
    echo "uv not found; installing into the current environment (pip install uv)." >&2
    python -m pip install uv
fi

# Generate production lockfile (universal, hash-pinned)
uv pip compile --universal --generate-hashes --no-strip-extras --no-header \
    --python-version 3.11 backend/requirements.txt \
    --output-file backend/requirements-lock.txt

# Generate CI lockfile (universal, hash-pinned)
uv pip compile --universal --generate-hashes --no-strip-extras --no-header \
    --python-version 3.11 backend/requirements-ci.txt \
    --output-file backend/requirements-lock-ci.txt

echo "Done. Diff the two lockfiles and justify every delta beyond markers/platform additions."
