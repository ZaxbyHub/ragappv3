#!/usr/bin/env bash
# Devcontainer bootstrap (issue #567 / E10): a known-good Linux environment
# matching CI (Python 3.11, Node 22.14, ubuntu) plus `just`, the task runner
# the repo justfile targets.
set -euo pipefail

# just 1.58.0 — the justfile at the repo root mirrors the CI gates.
curl -sSL -o /tmp/just.tar.gz \
  "https://github.com/casey/just/releases/download/1.58.0/just-1.58.0-x86_64-unknown-linux-musl.tar.gz"
tar -xzf /tmp/just.tar.gz -C /tmp just
sudo mv /tmp/just /usr/local/bin/just
just --version

# Backend: CI dependency set (universal lock, installs identically on Linux).
python -m pip install --upgrade pip
python -m pip install -r backend/requirements-lock-ci.txt -r backend/requirements-dev.txt

# Frontend: pinned toolchain.
cd frontend && npm ci --engine-strict

echo "Devcontainer ready. Run 'just --list' to see the CI-mirroring recipes."
