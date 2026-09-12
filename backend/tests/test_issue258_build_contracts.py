"""Issue #258 acceptance checks — build/CI/tooling contracts (E2, Phase 2.5).

One regression node per acceptance criterion, each printing an
``AC<n> CHECK: PASS`` / ``AC<n> CHECK: FAIL`` sentinel (visible under ``-s``)
so a plain ``python -m pytest tests/test_issue258_build_contracts.py -q -s``
run shows which contract holds on the current tree:

* AC8  (BUILD-001) — the Vite node-config project typechecks standalone
  (``tsc -p tsconfig.node.json --noEmit`` exits 0; no TS6307/TS2741).
* AC9  (BUILD-002 / C09+E07) — the runtime-version contract gate
  (``scripts/check_runtime_contract.py``, itself a deliverable of this issue),
  the CI ``push``(master)+``merge_group`` triggers, and dependabot
  ignore+cooldown for docker base-image majors.
* AC19 (ENH-001) — every lockfile entry carries ``--hash=sha256`` (preserving
  node: green at base and must stay green) and the lockfile regeneration
  procedure is documented (new surface).
* AC20 (ENH-003) — the dormant embedding-server code is gone: directories
  absent and no backend/app or scripts caller remains.

Junction/toolchain dependency (documented per the issue-tracer worktree setup):
the AC8 node shells out to the TypeScript compiler through
``frontend/node_modules``. In a linked worktree that directory is a junction to
the primary checkout's install; when it does not resolve the node falls back to
``npx tsc``. When no Node toolchain is on PATH at all the node SKIPS with an
explicit reason instead of false-failing — on any machine that can build the
frontend (dev boxes, CI frontend job) it runs for real.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"

_NODE = shutil.which("node")
_SUBPROCESS_TIMEOUT_SECONDS = 120

_LOCKFILE_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==")


def _run(argv: list[str], cwd: Path, timeout: int = _SUBPROCESS_TIMEOUT_SECONDS):
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


# ── AC8 (BUILD-001): node-config project typechecks ────────────────────────


def _node_config_typecheck_argv() -> list[str] | None:
    """Prefer the repo-local tsc (junction-safe); fall back to npx."""
    local_tsc = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"
    if _NODE and local_tsc.is_file():
        return [_NODE, str(local_tsc), "-p", "tsconfig.node.json", "--noEmit"]
    npx = shutil.which("npx")
    if npx:
        return [npx, "tsc", "-p", "tsconfig.node.json", "--noEmit"]
    return None


@pytest.mark.skipif(_node_config_typecheck_argv() is None, reason="no Node toolchain")
def test_ac8_vite_node_config_project_typechecks():
    try:
        # tsconfig.node.json is a composite project: even with --noEmit, tsc
        # writes a .tsbuildinfo next to the config unless redirected. Keep the
        # check side-effect-free by parking it in a temp dir.
        with tempfile.TemporaryDirectory() as scratch:
            argv = _node_config_typecheck_argv() + [
                "--tsBuildInfoFile",
                str(Path(scratch) / "tsconfig.node.tsbuildinfo"),
            ]
            print(f"argv: {argv}")
            result = _run(argv, FRONTEND)
        print(f"exit: {result.returncode}")
        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"  {line}")
        assert result.returncode == 0, (
            "frontend/tsconfig.node.json must typecheck standalone — "
            f"tsc exited {result.returncode} (TS6307 = include misses a file, "
            "TS2741 = missing required property)"
        )
    except Exception:
        print("AC8 CHECK: FAIL")
        raise
    print("AC8 CHECK: PASS")


# ── AC9 (BUILD-002): runtime contract + CI triggers + dependabot guards ─────


def test_ac9_runtime_contract_script_passes():
    try:
        script = REPO / "scripts" / "check_runtime_contract.py"
        argv = [sys.executable, str(script)]
        print(f"argv: {argv}")
        result = _run(argv, REPO)
        print(f"exit: {result.returncode}")
        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"  {line}")
        assert result.returncode == 0, (
            "scripts/check_runtime_contract.py must pass: Dockerfile FROM tags, "
            "ci.yml setup-node/setup-python, package.json engines and "
            "CONTRIBUTING.md must all agree with ALLOWED_RUNTIME"
        )
    except Exception:
        print("AC9 CHECK: FAIL")
        raise
    print("AC9 CHECK: PASS")


def _ci_triggers() -> dict:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    # PyYAML parses the YAML 1.1 boolean key `on:` as True.
    return workflow.get("on") or workflow.get(True) or {}


def test_ac9_ci_triggers_master_push_and_merge_group():
    try:
        triggers = _ci_triggers()
        print(f"triggers: {sorted(triggers)}")
        assert "pull_request" in triggers, "CI must keep pull_request triggers"
        push = triggers.get("push") or {}
        push_branches = push.get("branches", []) if isinstance(push, dict) else []
        print(f"push branches: {push_branches}")
        assert "push" in triggers and "master" in push_branches, (
            "CI must trigger on pushes to master, not only pull_request "
            "(BUILD-002 trigger half)"
        )
        assert "merge_group" in triggers, (
            "CI must validate merge groups (BUILD-002 trigger half)"
        )
    except Exception:
        print("AC9 CHECK: FAIL")
        raise
    print("AC9 CHECK: PASS")


def test_ac9_dependabot_base_image_major_ignore_and_cooldown():
    try:
        config = yaml.safe_load((REPO / ".github/dependabot.yml").read_text(encoding="utf-8"))
        docker_updates = [
            update
            for update in config.get("updates", [])
            if update.get("package-ecosystem") == "docker"
        ]
        print(f"docker ecosystems: {[u.get('directory') for u in docker_updates]}")
        assert docker_updates, "dependabot must keep watching docker base images"
        for update in docker_updates:
            directory = update.get("directory")
            ignores = update.get("ignore") or []
            ignored_types = {
                update_type
                for entry in ignores
                for update_type in entry.get("update-types", [])
            }
            print(f"{directory}: ignore={sorted(ignored_types)} cooldown={update.get('cooldown')}")
            assert "version-update:semver-major" in ignored_types, (
                f"dependabot docker entry {directory} must ignore major base-image "
                "bumps (the runtime contract gate owns major moves)"
            )
            cooldown = update.get("cooldown") or {}
            days = cooldown.get("default-days") or cooldown.get("major-days")
            assert days, (
                f"dependabot docker entry {directory} needs a cooldown "
                "(default-days or major-days) for base-image updates"
            )
    except Exception:
        print("AC9 CHECK: FAIL")
        raise
    print("AC9 CHECK: PASS")


# ── AC19 (ENH-001): hash-pinned lockfiles + documented regen procedure ──────


def _lockfile_entry_blocks(path: Path) -> list[list[str]]:
    """Entry = one ``name==version`` line plus its indented continuation lines.

    Unindented non-entry lines (header comments such as pip-tools' unsafe-libs
    banner) terminate the current block without counting as entries.
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if _LOCKFILE_ENTRY_RE.match(line):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif line[:1].isspace() and current is not None:
            current.append(line)
        else:
            if current is not None:
                blocks.append(current)
            current = None
    if current is not None:
        blocks.append(current)
    return blocks


def test_ac19_lockfile_entries_all_hash_pinned():
    """Preserving node: 100% hashed at base a543361 and must remain so."""
    try:
        for name in ("requirements-lock.txt", "requirements-lock-ci.txt"):
            path = REPO / "backend" / name
            blocks = _lockfile_entry_blocks(path)
            unhashed = [
                block[0]
                for block in blocks
                if not any("--hash=sha256" in line for line in block)
            ]
            print(f"{name}: {len(blocks)} entries, unhashed={unhashed}")
            assert blocks, f"{name} has no entries"
            assert not unhashed, f"{name} entries without --hash=sha256: {unhashed}"
    except Exception:
        print("AC19 CHECK: FAIL")
        raise
    print("AC19 CHECK: PASS")


def test_ac19_lockfile_regen_procedure_documented():
    try:
        procedure = REPO / "docs" / "engineering" / "lockfiles.md"
        assert procedure.is_file(), (
            "docs/engineering/lockfiles.md must document the lockfile "
            "regeneration procedure and the Windows-regeneration trap "
            "(ENH-001 gap: reproducibility procedure, not new lockfiles)"
        )
    except Exception:
        print("AC19 CHECK: FAIL")
        raise
    print("AC19 CHECK: PASS")


# ── AC20 (ENH-003): dormant embedding server removed ───────────────────────


def test_ac20_dormant_embedding_server_directories_absent():
    try:
        for relative in ("backend/embedding_server", "flag-embed-server"):
            path = REPO / relative
            print(f"{relative}: exists={path.exists()}")
            assert not path.exists(), (
                f"dormant embedding-server code must be removed (ENH-003): "
                f"{relative} still exists"
            )
    except Exception:
        print("AC20 CHECK: FAIL")
        raise
    print("AC20 CHECK: PASS")


def test_ac20_no_embedding_server_callers_in_backend_or_scripts():
    """rg-equivalent sweep of backend/app + scripts for lingering references.

    Excludes the bandit baseline and git history by construction (only
    ``*.py`` files under backend/app and scripts are read).
    ``scripts/check_runtime_contract.py`` is excluded: it NAMES the optional
    ``backend/embedding_server/Dockerfile`` surface to validate it — it is the
    removal gate, not a caller of the removed code.
    """
    try:
        gate_script = "scripts/check_runtime_contract.py"
        references: list[str] = []
        for base in (REPO / "backend" / "app", REPO / "scripts"):
            for path in base.rglob("*.py"):
                if (
                    "embedding_server" in path.read_text(encoding="utf-8", errors="replace")
                    and path.relative_to(REPO).as_posix() != gate_script
                ):
                    references.append(path.relative_to(REPO).as_posix())
        print(f"referencing files (excluding {gate_script}): {references}")
        assert not references, (
            "backend/app and scripts must not reference the removed "
            f"embedding_server: {references}"
        )
    except Exception:
        print("AC20 CHECK: FAIL")
        raise
    print("AC20 CHECK: PASS")
