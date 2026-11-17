"""Frozen issue-tracer checks for issue #753 (PowerShell launcher failures).

Acceptance checks AC1-AC4 of the #753 remediation plan:

- AC1: ``start-services.ps1`` must probe ALL containers (running or
  stopped) by exact name — not only running containers by substring — when
  deciding whether the embedding container already exists.
- AC2: when the embedding container exists but is stopped (host restart or
  ``docker compose stop``, which shares the ``harrier-embed`` name), the
  script must ``docker start`` it instead of attempting a conflicting
  ``docker run --name``.
- AC3: a failing ``docker run`` must be surfaced: no ``Out-Null`` on the
  run and a ``$LASTEXITCODE`` check right after it, instead of the
  misleading "may still be loading models" note.
- AC4: ``scripts/update-lock.ps1`` must check native exit codes
  (``$LASTEXITCODE`` after each native call, or
  ``$PSNativeCommandUseErrorActionPreference = $true``) so a failing
  ``uv pip compile`` stops the script instead of printing "Done.".

AC5 is NON-EXECUTABLE (host-only: both scripts are Windows PowerShell 5.1
operator tools and need a Docker daemon). AC6 is PRESERVING and stays
pinned by the existing suite nodes ``tests/test_issue258_build_contracts.py::
test_ac19_lockfile_regen_procedure_documented`` and ``::
test_ac19_lockfile_entries_all_hash_pinned`` — not re-implemented here.

AC1-AC4 are RED at the recorded base tree and turn GREEN when the #753
fix lands.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SERVICES = REPO_ROOT / "start-services.ps1"
UPDATE_LOCK_PS1 = REPO_ROOT / "scripts" / "update-lock.ps1"

_DOCKER_START_RE = re.compile(r"docker\s+start\b")
_NATIVE_CALL_RE = re.compile(r"^\s*&\s+(uv|python)\b")
_PSNATIVE_EAP_RE = re.compile(
    r"^\s*\$PSNativeCommandUseErrorActionPreference\s*=\s*\$true",
    re.MULTILINE,
)


# ── AC1: existence check sees stopped containers ────────────────────────────


def test_launcher_existence_check_sees_stopped_containers():
    text = START_SERVICES.read_text(encoding="utf-8")
    running_only_probes = []
    for line in text.splitlines():
        if "docker ps" not in line:
            continue
        if "$EmbeddingContainerName" not in line:
            continue
        tokens = line.split()
        if "-a" in tokens or "--all" in tokens:
            continue
        running_only_probes.append(line.strip())
    assert len(running_only_probes) == 0, (
        "start-services.ps1 must query all containers (docker ps -a/--all "
        "or docker inspect) when checking $EmbeddingContainerName; a "
        "running-only probe misses the stopped-container state and leads to "
        f"a docker run name conflict: {running_only_probes}"
    )


# ── AC2: a stopped container is started, not re-run ─────────────────────────


def test_launcher_starts_stopped_container():
    text = START_SERVICES.read_text(encoding="utf-8")
    starts = _DOCKER_START_RE.findall(text)
    assert len(starts) >= 1, (
        "start-services.ps1 must call 'docker start' for the stopped-"
        "container case: docker run --name harrier-embed fails with a name "
        "conflict after a host restart or 'docker compose stop' (compose "
        "uses the same container name)"
    )


# ── AC3: docker run failure is surfaced ─────────────────────────────────────


def test_launcher_docker_run_failure_is_surfaced():
    lines = START_SERVICES.read_text(encoding="utf-8").splitlines()
    run_lines = [
        index for index, line in enumerate(lines) if "docker @dockerArgs" in line
    ]
    ok = 0
    if run_lines:
        ok = 1
        for index in run_lines:
            window = lines[index + 1 : index + 6]  # the next 5 lines
            if "Out-Null" in lines[index] or not any(
                "$LASTEXITCODE" in later for later in window
            ):
                ok = 0
    assert ok == 1, (
        "the 'docker @dockerArgs' invocation must not be piped to Out-Null "
        "and must be followed by a $LASTEXITCODE check within 5 lines, so a "
        "failed docker run surfaces instead of the misleading 'may still be "
        "loading models' summary"
    )


# ── AC4: update-lock.ps1 checks native exit codes ───────────────────────────


def test_update_lock_ps1_checks_native_exit_codes():
    text = UPDATE_LOCK_PS1.read_text(encoding="utf-8")
    if _PSNATIVE_EAP_RE.search(text):
        # $PSNativeCommandUseErrorActionPreference = $true makes native
        # failures honor $ErrorActionPreference = 'Stop' — accepted fix.
        count = 0
    else:
        lines = text.splitlines()
        native_call_lines = [
            index
            for index, line in enumerate(lines)
            if _NATIVE_CALL_RE.match(line)
        ]
        count = 0
        for position, index in enumerate(native_call_lines):
            next_native = (
                native_call_lines[position + 1]
                if position + 1 < len(native_call_lines)
                else len(lines)
            )
            checked = any(
                "$LASTEXITCODE" in lines[probe]
                for probe in range(index + 1, next_native)
            )
            if not checked:
                count += 1
    assert count == 0, (
        "every native invocation in update-lock.ps1 (& uv / & python) must "
        "be followed by a $LASTEXITCODE test before the next native call "
        "(or $PSNativeCommandUseErrorActionPreference = $true): "
        f"{count} unchecked call(s); $ErrorActionPreference = 'Stop' does "
        "not govern native exit codes in Windows PowerShell 5.1"
    )
