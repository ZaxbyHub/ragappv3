#!/usr/bin/env python3
"""Check the runtime-version contract across Dockerfiles, CI, engines and docs.

Issue #258 (BUILD-002 / C09+E07): the repo pins its Node and Python runtimes
in six places that were only kept in agreement by comments that lied. This
script is the mechanical gate: every surface must agree with the ALLOWED_RUNTIME
table below, which is the single documented decision point for the contract.

Decision recorded in the #258 plan (root-cause trace, 04-root-cause.md):

* CI stays on Python 3.11 — the test suite's 3.11 pin is load-bearing
  (see docs/engineering/testing.md for the newer-interpreter event-loop
  artifact); Docker images are re-pinned to python:3.11 so a green CI run
  proves the shipped image builds and runs.
* Node targets major 22 (current LTS) — Node 20 reached EOL 2026-04-30;
  ci.yml, both Dockerfiles, package.json engines and CONTRIBUTING.md are
  aligned on 22.

Surfaces checked (each line lists one runtime mention -> required value):

  Dockerfile                          FROM node:<major>-...  -> node major
                                      FROM python:<X.Y>-...  -> python version
  frontend/Dockerfile                 FROM node:<major>-...  -> node major
  backend/embedding_server/Dockerfile FROM python:<X.Y>-...  -> python version
                                      (optional surface: skipped when absent)
  .github/workflows/ci.yml            setup-node `node-version`     -> node major
                                      setup-python `python-version` -> python version
  frontend/package.json               engines.node minimum major    -> node major
  CONTRIBUTING.md                     "Node.js <X.Y>" mentions      -> node major
                                      "python<X.Y>" mentions        -> python version

Digest pins (@sha256:...) are honored: the tag before the digest is compared.
Non-runtime base images (nginx, ollama, ...) are ignored.

Stdlib-only on purpose: the CI Quality contracts job runs scripts/check_*.py
with a bare setup-python and no dependency install, matching the convention of
scripts/check_config_contract.py.

Exit 0 = every surface agrees; exit 1 with one ``runtime-contract:`` line per
mismatch on stderr. Run from anywhere (paths resolve from this file).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── ALLOWED pin table ──────────────────────────────────────────────────────
# THE decision point for the runtime contract. To move the project to a new
# runtime: update this table, then re-align every surface listed in the
# module docstring to match. Nothing else in this file encodes versions.
ALLOWED_RUNTIME: dict[str, dict[str, str]] = {
    # Plan-critic R1 item 7: the Node pin is MINOR-EXACT (e.g. "22.14.0") so the
    # contract asserts the same minor across ci.yml setup-node, package.json
    # engines (>=22.14.0), Dockerfile FROM tags, and CONTRIBUTING.md.
    "node": {"major": "22", "minor": "14"},
    "python": {"version": "3.11"},
}

# Dockerfiles whose FROM lines pin a tracked runtime. backend/embedding_server
# is an optional surface: when the dormant sidecar is removed (ENH-003) the
# file disappears and the check simply skips it.
DOCKERFILE_SURFACES = (
    "Dockerfile",
    "frontend/Dockerfile",
    "backend/embedding_server/Dockerfile",
)

FROM_RE = re.compile(
    r"(?im)^\s*FROM\s+"
    r"(?:--[\w-]+=\S+\s+)?"  # optional --platform=... style flags
    r"(?P<image>[a-z0-9][a-z0-9./_-]*):"
    r"(?P<tag>[A-Za-z0-9._-]+)"
    r"(?:@sha256:[0-9a-f]+)?"
)

CI_NODE_VERSION_RE = re.compile(r"(?im)^\s*node-version:\s*[\"']?(\d+(?:\.\d+)*)[\"']?\s*$")
CI_PYTHON_VERSION_RE = re.compile(
    r"(?im)^\s*python-version:\s*[\"']?(\d+(?:\.\d+)*)[\"']?\s*$"
)

CONTRIBUTING_NODE_RE = re.compile(r"(?i)\bnode(?:\.js)?\b[^\n]{0,40}?\b(\d{1,2})\.\d+")
CONTRIBUTING_PYTHON_RE = re.compile(r"(?i)\bpython(?:3)?[ @:]\s*(\d+\.\d+)")


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def fail(message: str) -> None:
    print(f"runtime-contract: {message}", file=sys.stderr)


def tag_version(tag: str) -> str:
    """Strip the variant suffix from an image tag: ``3.14-slim`` -> ``3.14``."""
    return tag.split("-")[0]


def from_statements(dockerfile_text: str) -> list[tuple[str, str]]:
    """Return (image, version) for every FROM line with a numeric tag."""
    result: list[tuple[str, str]] = []
    for match in FROM_RE.finditer(dockerfile_text):
        tag = match.group("tag")
        version = tag_version(tag)
        if not re.fullmatch(r"\d+(\.\d+)*", version):
            continue  # nginx:alpine / ollama:latest style — not a tracked runtime
        result.append((match.group("image"), version))
    return result


def _expected_node() -> str:
    """Minor-exact node pin, e.g. '22.11'."""
    n_ = ALLOWED_RUNTIME["node"]
    return f"{n_['major']}.{n_['minor']}"


def _node_version_ok(version: str) -> tuple[bool, str]:
    """True when a pinned node version string matches the minor-exact contract."""
    digits = re.search(r"(\d+)\.(\d+)", version)
    if not digits:
        return False, f"unparseable node version {version!r}"
    major, minor = digits.group(1), digits.group(2)
    exp = ALLOWED_RUNTIME["node"]
    if major != exp["major"] or minor != exp["minor"]:
        return False, f"node {major}.{minor}, contract requires {_expected_node()}.x"
    return True, ""


def check_dockerfiles(failures: list[str]) -> None:
    for surface in DOCKERFILE_SURFACES:
        path = ROOT / surface
        if not path.is_file():
            continue
        for image, version in from_statements(path.read_text(encoding="utf-8")):
            if image == "node":
                ok, why = _node_version_ok(version)
                if not ok:
                    failures.append(f"{surface}: FROM {image}:{version} is {why}")
            elif image == "python":
                if version != ALLOWED_RUNTIME["python"]["version"]:
                    failures.append(
                        f"{surface}: FROM {image}:{version} is python {version}, "
                        f"contract requires {ALLOWED_RUNTIME['python']['version']}"
                    )


def check_ci_versions(failures: list[str]) -> None:
    ci_text = read(".github/workflows/ci.yml")
    for match in CI_NODE_VERSION_RE.finditer(ci_text):
        version = match.group(1)
        ok, why = _node_version_ok(version)
        if not ok:
            failures.append(
                f".github/workflows/ci.yml: setup-node node-version {version!r} is {why}"
            )
    for match in CI_PYTHON_VERSION_RE.finditer(ci_text):
        version = match.group(1)
        if version != ALLOWED_RUNTIME["python"]["version"]:
            failures.append(
                f".github/workflows/ci.yml: setup-python python-version {version!r} "
                f"contract requires {ALLOWED_RUNTIME['python']['version']!r}"
            )


def check_package_engines(failures: list[str]) -> None:
    package = json.loads(read("frontend/package.json"))
    engines_node = package.get("engines", {}).get("node")
    if not engines_node:
        failures.append("frontend/package.json: engines.node is missing")
        return
    match = re.search(r"\d+", engines_node)
    if not match:
        failures.append(
            f"frontend/package.json: engines.node {engines_node!r} has no parseable "
            "minimum version"
        )
        return
    digits = re.search(r"(\d+)\.(\d+)", engines_node)
    if not digits:
        failures.append(
            f"frontend/package.json: engines.node {engines_node!r} lacks a "
            "major.minor pin"
        )
        return
    major, minor = digits.group(1), digits.group(2)
    exp = ALLOWED_RUNTIME["node"]
    # engines is a FLOOR: within the contract major, the minor must be >= the
    # contract minor (a higher floor is still contract-compliant).
    if major != exp["major"] or int(minor) < int(exp["minor"]):
        failures.append(
            f"frontend/package.json: engines.node {engines_node!r} floors node "
            f"{major}.{minor}, contract requires >={_expected_node()}"
        )


def check_contributing(failures: list[str]) -> None:
    text = read("CONTRIBUTING.md")
    node_re = re.compile(
        r"(?i)\bnode(?:\.js)?\b[^\n]{0,40}?\b(\d{1,2})\.(\d+)"
    )
    for match in node_re.finditer(text):
        ok, why = _node_version_ok(f"{match.group(1)}.{match.group(2)}")
        if not ok:
            failures.append(
                "CONTRIBUTING.md: Node.js prerequisite mention "
                f"{match.group(0).strip()!r} is {why}"
            )
    for match in CONTRIBUTING_PYTHON_RE.finditer(text):
        version = match.group(1)
        if version != ALLOWED_RUNTIME["python"]["version"]:
            failures.append(
                "CONTRIBUTING.md: Python prerequisite mention "
                f"{match.group(0).strip()!r} is python {version}, contract "
                f"requires {ALLOWED_RUNTIME['python']['version']}"
            )


def main() -> int:
    failures: list[str] = []
    check_dockerfiles(failures)
    check_ci_versions(failures)
    check_package_engines(failures)
    check_contributing(failures)
    for message in failures:
        fail(message)
    if failures:
        return 1
    print("runtime-contract: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
