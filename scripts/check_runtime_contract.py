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
  .devcontainer/devcontainer.json     features python/node versions -> python version,
                                      node major.minor (required surface since issue
                                      #567; the container mirrors CI's runtimes)
  docs/engineering/conventions.md     runtime-pin prose (Node/Python/Vitest/Vite)
                                      -> ALLOWED_RUNTIME + package.json vitest/vite
                                      majors; CI job-name inventory -> ci.yml
  docs/engineering/testing.md         CI job-name + quality-contract script
                                      inventories -> ci.yml ground truth
                                      (both docs surfaces are required since
                                      issue #655: a missing file fails instead
                                      of being skipped)

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
    # Plan-critic R1 item 7: the Node pin is MINOR-EXACT (e.g. "22.22.0") so the
    # contract asserts the same minor across ci.yml setup-node, package.json
    # engines (>=22.22.0), Dockerfile FROM tags, and CONTRIBUTING.md.
    "node": {"major": "22", "minor": "22"},
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

# Docs surfaces (issue #655): the engineering docs assert runtime facts and a
# CI job/script inventory that this gate now keeps true. Prose forms are matched
# with the same windowed style as the CONTRIBUTING family above, extended with
# `.x` wildcard majors and `@`-style dependency mentions.
DOCS_SURFACES = (
    "docs/engineering/conventions.md",
    "docs/engineering/testing.md",
)

DOC_NODE_PROSE_RE = re.compile(r"\bnode(?:\.js)?\b[^\n]{0,40}?[\s(\"]v?(\d{1,2})\.(\d+|x)\b", re.IGNORECASE)
DOC_NODE_OP_RE = re.compile(r"(?i)\bnode(?:\.js)?\s*(?:version\s*)?[><=]=?\s*(\d+)(?:\.(\d+|x))?\b")
DOC_PYTHON_RE = CONTRIBUTING_PYTHON_RE
# Version adjacency required (only separators like space/@/(/)/</>/=/+ may sit
# between the name and the digits), so "vitest requires Node 22" cannot be
# misread as a vitest version claim. `^~v` are included because that is the
# exact style frontend/package.json itself uses ("vitest": "~5.0.0") — a stale
# pin written in package.json style must still be caught.
DOC_VITEST_RE = re.compile(r"(?i)\bvitest\b[ @()<>=+^~v]*(\d+)(?:\.(\d+|x))?\b")
DOC_VITE_RE = re.compile(r"(?i)\bvite\b[ @()<>=+^~v]*(\d+)(?:\.(\d+|x))?\b")
DOC_SCRIPT_RE = re.compile(r"scripts/check_[a-z_]+\.py")


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


def _feature_version(value: object) -> str:
    """Feature pins are either a bare version string or {"version": "..."}."""
    if isinstance(value, dict):
        return str(value.get("version", "")).strip()
    return str(value).strip()


def check_devcontainer(failures: list[str]) -> None:
    """The devcontainer mirrors CI's runtimes; its feature pins are contract
    surfaces (issue #567). Unlike the optional embedding_server Dockerfile,
    this file is required: it ships in the same change that made it a
    surface."""
    path = ROOT / ".devcontainer" / "devcontainer.json"
    if not path.is_file():
        failures.append(
            ".devcontainer/devcontainer.json: missing (required runtime-"
            "contract surface since issue #567)"
        )
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f".devcontainer/devcontainer.json: invalid JSON: {exc}")
        return
    features = doc.get("features")
    if not isinstance(features, dict):
        failures.append(".devcontainer/devcontainer.json: no features map")
        return
    surface = ".devcontainer/devcontainer.json"
    python_pin = features.get("ghcr.io/devcontainers/features/python:1")
    if python_pin is None:
        failures.append(f"{surface}: python feature is missing")
    elif _feature_version(python_pin) != ALLOWED_RUNTIME["python"]["version"]:
        failures.append(
            f"{surface}: python feature {python_pin!r}, contract requires "
            f"{ALLOWED_RUNTIME['python']['version']!r}"
        )
    node_pin = features.get("ghcr.io/devcontainers/features/node:1")
    if node_pin is None:
        failures.append(f"{surface}: node feature is missing")
    else:
        ok, why = _node_version_ok(_feature_version(node_pin))
        if not ok:
            failures.append(f"{surface}: node feature {node_pin!r} is {why}")


def _dep_major(package_text: str, key: str, failures: list[str]) -> str | None:
    """Return the major of a frontend/package.json dependency, or None (fail-loud)."""
    match = re.search(rf'"{key}"\s*:\s*"[~^]?(\d+)', package_text)
    if not match:
        failures.append(
            f"frontend/package.json: no parseable {key} version for the "
            "docs-surface comparison"
        )
        return None
    return match.group(1)


def _flag_node_doc_mention(
    failures: list[str], mention: str, whole: str, frac: str | None
) -> None:
    """Flag a conventions.md Node mention that breaks the minor-exact contract.

    A ``.x`` fraction (or a bare major from an operator form) is a major-only
    claim: it passes when the major matches. A concrete ``major.minor`` must
    satisfy the minor-exact contract.
    """
    if frac is None or frac == "x":
        expected = ALLOWED_RUNTIME["node"]["major"]
        if whole != expected:
            failures.append(
                f"docs/engineering/conventions.md: runtime mention {mention!r} is "
                f"node major {whole}, contract requires {expected}.x"
            )
        return
    ok, why = _node_version_ok(f"{whole}.{frac}")
    if not ok:
        failures.append(
            f"docs/engineering/conventions.md: runtime mention {mention!r} is {why}"
        )


def _strip_yaml_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _ci_job_display_names(ci_text: str) -> list[str]:
    """Job display names from a workflow's ``jobs:`` mapping.

    Stdlib indentation walk (the Quality contracts job has no YAML parser):
    a job's display name is its ``name:`` value — quotes stripped, whatever
    the indentation inside ``jobs:`` — falling back to the job key when a job
    declares no ``name:``. Returns [] when ``jobs:`` is absent or empty so the
    caller can fail loud instead of comparing against a silently truncated
    inventory.
    """
    names: list[str] = []
    in_jobs = False
    job_indent: int | None = None
    pending: str | None = None
    for raw in ci_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = _strip_yaml_quotes(key.strip())
        value = value.strip()
        if not in_jobs:
            if indent == 0 and key == "jobs" and value == "":
                in_jobs = True
            continue
        if indent == 0:
            break  # the next top-level key ends the jobs: mapping
        if job_indent is None:
            job_indent = indent
        if indent == job_indent:
            if pending is not None:
                names.append(pending)
            pending = key  # resolved by the job's own `name:` child when present
        elif pending is not None and indent == job_indent + 2 and key == "name" and value:
            # An empty resolved name (e.g. `name: ''`) would make the substring
            # inventory check vacuous ('' in anything is True) — fall back to
            # the job key so the job stays visible to the doc checks.
            pending = _strip_yaml_quotes(value) or pending
    if pending is not None:
        names.append(pending)
    return names


def docs_surface_failures(
    conventions_text: str, testing_text: str, ci_text: str
) -> list[str]:
    """Pure docs-surface check (issue #655; the frozen acceptance-driver contract).

    Flags runtime-pin prose in conventions.md (Node/Python against ALLOWED_RUNTIME,
    Vitest/Vite majors against the live frontend/package.json) and CI job/script
    inventory drift in conventions.md + testing.md against ci.yml ground truth.
    Every failure names the offending doc file and the specific offending item in
    the same string. Reads frontend/package.json for the dependency majors; a
    malformed package.json fails loud instead of raising.
    """
    failures: list[str] = []
    label = "docs/engineering/conventions.md"

    package_path = ROOT / "frontend" / "package.json"
    package_text = (
        package_path.read_text(encoding="utf-8") if package_path.is_file() else ""
    )
    vite_major = _dep_major(package_text, "vite", failures)
    vitest_major = _dep_major(package_text, "vitest", failures)

    seen: set[str] = set()
    for match in DOC_NODE_PROSE_RE.finditer(conventions_text):
        ok, why = (
            (True, "")
            if match.group(2) == "x"
            else _node_version_ok(f"{match.group(1)}.{match.group(2)}")
        )
        if match.group(2) == "x" and match.group(1) != ALLOWED_RUNTIME["node"]["major"]:
            ok, why = False, (
                f"node major {match.group(1)}, contract requires "
                f"{ALLOWED_RUNTIME['node']['major']}.x"
            )
        if ok:
            continue
        mention = match.group(0).strip()
        if mention not in seen:
            seen.add(mention)
            failures.append(f"{label}: runtime mention {mention!r} is {why}")
    for match in DOC_NODE_OP_RE.finditer(conventions_text):
        _flag_node_doc_mention(
            failures, match.group(0).strip(), match.group(1), match.group(2)
        )
    for match in DOC_PYTHON_RE.finditer(conventions_text):
        if match.group(1) != ALLOWED_RUNTIME["python"]["version"]:
            failures.append(
                f"{label}: runtime mention {match.group(0).strip()!r} is python "
                f"{match.group(1)}, contract requires "
                f"{ALLOWED_RUNTIME['python']['version']}"
            )
    if vitest_major is not None:
        for match in DOC_VITEST_RE.finditer(conventions_text):
            if match.group(1) != vitest_major:
                failures.append(
                    f"{label}: runtime mention {match.group(0).strip()!r} is "
                    f"vitest major {match.group(1)}, contract requires "
                    f"{vitest_major} (frontend/package.json)"
                )
    if vite_major is not None:
        for match in DOC_VITE_RE.finditer(conventions_text):
            if match.group(1) != vite_major:
                failures.append(
                    f"{label}: runtime mention {match.group(0).strip()!r} is "
                    f"vite major {match.group(1)}, contract requires "
                    f"{vite_major} (frontend/package.json)"
                )

    job_names = _ci_job_display_names(ci_text)
    if not job_names:
        failures.append(
            "no job names parsed from .github/workflows/ci.yml — parser or "
            "workflow drift"
        )
    else:
        for name in job_names:
            if name not in conventions_text:
                failures.append(
                    f"{label}: CI job {name!r} missing from the documented "
                    "job inventory"
                )
            if name not in testing_text:
                failures.append(
                    "docs/engineering/testing.md: CI job "
                    f"{name!r} missing from the documented job inventory"
                )
    script_names = sorted(set(DOC_SCRIPT_RE.findall(ci_text)))
    if not script_names:
        failures.append(
            "no scripts/check_*.py gates parsed from .github/workflows/ci.yml "
            "— parser or workflow drift"
        )
    else:
        for name in script_names:
            if name not in testing_text:
                failures.append(
                    "docs/engineering/testing.md: contract script "
                    f"{name!r} missing from the documented quality-contracts "
                    "inventory"
                )
    return failures


def check_docs_surfaces(failures: list[str]) -> None:
    """Required docs surfaces (issue #655): missing files fail, never skip."""
    texts: dict[str, str] = {}
    for surface in DOCS_SURFACES:
        path = ROOT / surface
        if not path.is_file():
            failures.append(
                f"{surface} missing (required runtime-contract surface since "
                "issue #655)"
            )
        else:
            texts[surface] = path.read_text(encoding="utf-8")
    ci_path = ROOT / ".github" / "workflows" / "ci.yml"
    if not ci_path.is_file():
        failures.append(
            ".github/workflows/ci.yml missing (required runtime-contract surface)"
        )
        return
    if len(texts) < len(DOCS_SURFACES):
        return
    failures.extend(
        docs_surface_failures(
            texts[DOCS_SURFACES[0]],
            texts[DOCS_SURFACES[1]],
            ci_path.read_text(encoding="utf-8"),
        )
    )


def main() -> int:
    failures: list[str] = []
    check_dockerfiles(failures)
    check_ci_versions(failures)
    check_package_engines(failures)
    check_contributing(failures)
    check_devcontainer(failures)
    check_docs_surfaces(failures)
    for message in failures:
        fail(message)
    if failures:
        return 1
    print("runtime-contract: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
