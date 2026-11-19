"""Frozen issue-tracer checks for issue #752 (README dev loop and build docs).

Acceptance checks AC1-AC5 of the #752 remediation plan:

- AC1: every ``-f <file>.yml`` compose file README.md names must be tracked
  in the repository (``docker-compose.override.yml`` was deleted, so the
  documented hot-reload command fails as written).
- AC2: every ``docker compose logs|exec|restart|stop|start`` service name
  README.md cites must be defined in a tracked ``docker-compose*.yml``
  (no service named ``backend`` has ever existed).
- AC3: README.md must not instruct running ``pytest`` inside a container:
  the runtime image excludes ``backend/tests/`` and installs no pytest.
- AC4: Dockerfile build comments must not cite a literal ci.yml
  ``node-version`` value that differs from what ci.yml actually pins.
- AC5: the root image must not create the unused ``/data/knowledgevault``
  directory (``DATA_DIR`` resolves to ``/app/data``).

AC4 and AC5 are GREEN at the recorded base tree (the stale comments and
the unused mkdir were already corrected there) and stay GREEN; AC1-AC3 are
RED at the base tree and turn GREEN when the #752 fix lands. AC6 is
PRESERVING (pinned by the existing ``test_issue258_build_contracts.py`` /
``test_runtime_contract_docs.py`` suites, not re-implemented here) and AC7
is NON-EXECUTABLE (host-only: needs a Docker host).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
ROOT_DOCKERFILE = REPO_ROOT / "Dockerfile"
FRONTEND_DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"
CI_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_COMPOSE_FILE_ARG_RE = re.compile(r"-f\s+(\S+\.ya?ml)")
_COMPOSE_SERVICE_CMD_RE = re.compile(
    r"docker compose (?:logs|exec|restart|stop|start)(?:\s+-{1,2}[\w-]+)*"
    r"\s+([a-z][\w-]*)"
)
_PYTEST_IN_CONTAINER_RE = re.compile(
    r"docker compose exec\s+\S+\s+(python -m )?pytest"
)
_STALE_NODE_VERSION_RE = re.compile(r'node-version "(\d+\.\d+\.\d+)"')
_CI_NODE_VERSION_RE = re.compile(r'node-version:\s*"?([^\s"\n]+)"?')
_UNUSED_DATA_DIR_RE = re.compile(r"^\s*RUN .*\bmkdir\b.*/data/knowledgevault")


def _compose_service_names() -> set[str]:
    """Union of service names across tracked ``docker-compose*.yml`` files.

    Deliberately a line walk, not ``yaml.safe_load``:
    docker-compose.gpu-pins.yml uses the ``!override`` custom tag, which
    safe_load rejects. Service names are the exactly-two-space-indented
    keys between the top-level ``services:`` line and the next column-0
    key.
    """
    names: set[str] = set()
    for compose_file in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        in_services = False
        for line in compose_file.read_text(encoding="utf-8").splitlines():
            if not in_services:
                if line.rstrip() == "services:" and not line.startswith(" "):
                    in_services = True
                continue
            if not line.strip():
                continue
            if not line.startswith(" "):
                # Only a column-0 key ends the top-level services: block —
                # deeper-indented lines (4+ spaces) are service PROPERTIES,
                # not block terminators.
                in_services = False
            elif line.startswith("  ") and not line.startswith("   "):
                match = re.match(r"^  ([^#\s][^:\s]*):", line)
                if match:
                    names.add(match.group(1))
    return names


# ── AC1: README compose files exist ─────────────────────────────────────────


def test_readme_compose_files_exist():
    readme = README.read_text(encoding="utf-8")
    missing = []
    for line in readme.splitlines():
        if "docker compose" not in line:
            continue
        for match in _COMPOSE_FILE_ARG_RE.finditer(line):
            compose_file = match.group(1)
            if not (REPO_ROOT / compose_file).exists():
                missing.append(compose_file)
    assert len(missing) == 0, (
        "README.md 'docker compose -f' commands must name tracked compose "
        f"files; missing from the repo: {missing}"
    )


# ── AC2: README compose service names exist ─────────────────────────────────


def test_readme_compose_service_names_exist():
    readme = README.read_text(encoding="utf-8")
    defined = _compose_service_names()
    undefined = [
        name
        for name in _COMPOSE_SERVICE_CMD_RE.findall(readme)
        if name not in defined
    ]
    assert len(undefined) == 0, (
        "README.md 'docker compose logs|exec|restart|stop|start' commands "
        f"must name services defined in docker-compose*.yml (services: "
        f"{sorted(defined)}); undefined cited: {undefined}"
    )


# ── AC3: README must not run pytest inside a container ──────────────────────


def test_readme_does_not_run_pytest_in_container():
    readme = README.read_text(encoding="utf-8")
    occurrences = _PYTEST_IN_CONTAINER_RE.findall(readme)
    assert len(occurrences) == 0, (
        "README.md must not instruct 'docker compose exec ... pytest': the "
        "runtime image excludes backend/tests/ (.dockerignore) and installs "
        f"no pytest pin ({len(occurrences)} occurrence(s))"
    )


# ── AC4: Dockerfile comments cite the real ci.yml node version ──────────────


def test_dockerfile_comments_do_not_cite_stale_ci_node_version():
    ci_text = CI_FILE.read_text(encoding="utf-8")
    pinned = set(_CI_NODE_VERSION_RE.findall(ci_text))
    assert pinned, "ci.yml must pin node-version values"

    stale = []
    for dockerfile in (ROOT_DOCKERFILE, FRONTEND_DOCKERFILE):
        text = dockerfile.read_text(encoding="utf-8")
        for match in _STALE_NODE_VERSION_RE.finditer(text):
            if match.group(1) not in pinned:
                stale.append(f"{dockerfile.name}: {match.group(0)}")
    assert len(stale) == 0, (
        f"Dockerfile build comments must not cite node-version values that "
        f"differ from ci.yml's pins ({sorted(pinned)}); stale: {stale}"
    )


# ── AC5: root image does not create the unused /data/knowledgevault ─────────


def test_root_image_does_not_create_unused_data_dir():
    text = ROOT_DOCKERFILE.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if _UNUSED_DATA_DIR_RE.search(line)
    ]
    assert len(offenders) == 0, (
        "the root Dockerfile must not create the unused "
        f"/data/knowledgevault directory (DATA_DIR is /app/data in compose "
        f"and ./data under WORKDIR /app in config.py): {offenders}"
    )
