"""Frozen issue-tracer checks for issue #748 (container lifecycle).

Acceptance checks C1/C2/C4/C5 of the #748 remediation plan:

- C1: a container stop must deliver SIGTERM to uvicorn — the image runs it
  as PID 1 (or under an exec/init that forwards signals), not as the child
  of a non-exec ``sh -c``.
- C2 (PRESERVING pin): the ``knowledgevault`` stop grace period must stay
  above the 60 s ``BackgroundProcessor.stop`` drain budget. This landed via
  issue #700's follow-up (``stop_grace_period: 90s``); the pin keeps it.
- C4: ``knowledgevault`` must wait for a healthy ``harrier-embed`` before
  starting, so the strict TEI model check runs against a ready service.
- C5: the image command must pin a single uvicorn worker (``--workers 1``)
  so an operator-set ``WEB_CONCURRENCY`` cannot silently enable an
  unsupported multi-process topology.

Each check is RED at the recorded base tree and turns GREEN when the #748
fix lands; C2 is GREEN at base by design.
"""

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

_DOCKER_DEFAULT_STOP_GRACE_SECONDS = 10


def _dockerfile_argv(directive):
    """JSON-array arguments of every ``directive`` line, in file order."""
    argvs = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text.startswith(directive + " "):
            continue
        payload = text[len(directive):].strip()
        try:
            argv = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(argv, list):
            argvs.append(argv)
    return argvs


def _compose_services():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    return compose.get("services", {})


def _grace_seconds(raw):
    """Parse a compose ``stop_grace_period`` into seconds.

    Accepts the ``90s`` and ``1m30s`` documented forms (plus bare numbers
    and other single-unit suffixes); Docker's default of 10 s applies when
    the key is absent.
    """
    if raw is None:
        return _DOCKER_DEFAULT_STOP_GRACE_SECONDS
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip().lower()
    minutes_and_seconds = re.fullmatch(r"(\d+)m(\d+)s", text)
    if minutes_and_seconds:
        return (
            int(minutes_and_seconds.group(1)) * 60
            + int(minutes_and_seconds.group(2))
        )
    single_unit = re.fullmatch(r"(\d+)([smh]?)", text)
    if single_unit:
        multiplier = {"": 1, "s": 1, "m": 60, "h": 3600}[
            single_unit.group(2)
        ]
        return int(single_unit.group(1)) * multiplier
    return _DOCKER_DEFAULT_STOP_GRACE_SECONDS


def test_uvicorn_receives_sigterm_as_pid1_or_via_init():
    cmd_argv = _dockerfile_argv("CMD")
    assert cmd_argv, "Dockerfile must define a CMD"
    argv = cmd_argv[-1]

    entrypoint_argv = _dockerfile_argv("ENTRYPOINT")
    knowledgevault = _compose_services().get("knowledgevault", {}) or {}

    ok = 0
    # uvicorn itself is PID 1 (no shell wrapper).
    if argv[0] not in ("sh", "/bin/sh", "bash"):
        ok = 1
    # A shell wrapper that execs uvicorn replaces itself with uvicorn.
    if "-c" in argv:
        script_index = argv.index("-c") + 1
        script = argv[script_index] if script_index < len(argv) else ""
        if script.startswith("exec "):
            ok = 1
    # An init process forwards SIGTERM to the application.
    if any(
        "tini" in token or "dumb-init" in token
        for one_entrypoint in entrypoint_argv
        for token in one_entrypoint
    ):
        ok = 1
    # Compose's built-in init (`docker run --init` equivalent).
    if knowledgevault.get("init") is True:
        ok = 1
    assert ok == 1


def test_knowledgevault_stop_grace_exceeds_drain_budget():
    knowledgevault = _compose_services().get("knowledgevault", {}) or {}
    grace = _grace_seconds(knowledgevault.get("stop_grace_period"))
    assert grace > 60


def test_knowledgevault_waits_for_healthy_embedder():
    knowledgevault = _compose_services().get("knowledgevault", {}) or {}
    depends_on = knowledgevault.get("depends_on") or {}
    if isinstance(depends_on, list):
        depends_on = {name: {} for name in depends_on}
    assert depends_on.get("harrier-embed", {}).get("condition") == "service_healthy"


def test_image_cmd_pins_single_worker():
    cmd_argv = _dockerfile_argv("CMD")
    assert cmd_argv, "Dockerfile must define a CMD"
    cmd = " ".join(cmd_argv[-1])
    assert int(bool(re.search(r"--workers[ =]1\b", cmd))) == 1
