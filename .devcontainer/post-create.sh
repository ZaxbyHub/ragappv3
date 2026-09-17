#!/usr/bin/env bash
# Devcontainer bootstrap (issue #567 / E10): a known-good Linux environment
# matching CI (Python 3.11, Node 22.14, ubuntu) plus the tools the justfile
# recipes need: just, uv (pinned to the same version CI's lock-verification
# gate pins), and pytest-xdist (installed ad hoc by CI, ci.yml).
set -euo pipefail

# just 1.58.0 — the justfile at the repo root mirrors the CI gates.
# Digest hardcoded from the release's published SHA256SUMS (matching the
# repo's hash-everything posture: --require-hashes, devcontainer-lock.json).
JUST_SHA256=4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d
curl -sSL -o /tmp/just.tar.gz \
  "https://github.com/casey/just/releases/download/1.58.0/just-1.58.0-x86_64-unknown-linux-musl.tar.gz"
echo "$JUST_SHA256  /tmp/just.tar.gz" | sha256sum -c -
tar -xzf /tmp/just.tar.gz -C /tmp just
sudo mv /tmp/just /usr/local/bin/just
just --version

# Backend: CI dependency set (universal lock, installs identically on Linux;
# the lock install is hash-gated like CI and the Dockerfile — issue #567).
python -m pip install --upgrade pip
python -m pip install "uv==0.12.15"
python -m pip install --require-hashes -r backend/requirements-lock-ci.txt
python -m pip install -r backend/requirements-dev.txt
# pytest-xdist is installed ad hoc by CI (ci.yml) and is required by the
# justfile's backend-test recipe (-n auto).
python -m pip install pytest-xdist

# Frontend: pinned toolchain.
cd frontend && npm ci --engine-strict

echo "Devcontainer ready. Run 'just --list' to see the CI-mirroring recipes."
