#!/usr/bin/env python3
"""Frozen AC1 mutation check for issue #787 (GX-026).

Issue #787's audit found that ``_stage_input_hash``
(backend/app/services/draft_pipeline.py) folds every stage-specific payload
(the candidate hash, the outline artifact hash, ...) into the stage input
hash via a ``**payload,`` splat inside the hashed ``body`` dict. GX-026 is
the audit's mutation that deletes that splat: with it gone, two stages that
consume different inputs hash identically, and a checkpoint written from one
set of inputs can be reused for another. Only a test that varies an upstream
artifact hash and asserts the downstream stage re-runs can tell the mutated
pipeline from the real one.

This script makes that difference mechanical:

1. Copy ``backend/`` into two throwaway trees (clean and mutated).
2. In the mutated copy only, delete the single ``**payload,`` line from
   ``app/services/draft_pipeline.py`` (aborting if it does not occur exactly
   once, so a refactor cannot silently neuter the mutation).
3. Run the frozen acceptance surface — ``test_draft_pipeline.py`` plus every
   ``test_n03_*.py`` check file discovered INSIDE each copy, so a post-fix
   check file is picked up automatically — in both trees.
4. Compare the failing node-id sets. Identical sets mean the mutant
   SURVIVED (exit 1); any mutant-only failure means it was KILLED (exit 0).

Run from the repo root:

    python scripts/check_n03_gx026_mutant.py

Exit codes: 0 = KILLED, 1 = SURVIVED, 3 = mutation site not found exactly
once (the script refuses to run against a tree it cannot mutate reliably).
"""

import glob
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The exact mutated line: the ``**payload,`` splat in ``_stage_input_hash``.
MUTANT_LINE = "        **payload,\n"

#: Copied trees exclude caches and virtualenvs: they are large, machine
#: specific, and would poison the subprocess's import resolution.
COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".venv*",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)

PYTEST_ARGS = ["-q", "-p", "no:randomly", "-p", "no:cacheprovider", "-rf"]


def _make_temp_root() -> Path:
    """First writable candidate among the known scratch locations."""
    for candidate in ("E:/tmp", "C:/Windows/Temp", str(REPO / ".swarm")):
        try:
            Path(candidate).mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="n03-gx026-", dir=candidate))
        except OSError:
            continue
    raise SystemExit("ABORT: no writable temp root among the candidates")


def _testfiles(copy_root: Path) -> list:
    """The frozen acceptance surface, discovered inside one copied tree.

    ``test_n03_*.py`` files are globbed relative to the copy so a check file
    added by the Phase-4 fix is picked up without editing this script.
    """
    files = ["tests/draft_room/test_draft_pipeline.py"]
    files.extend(
        Path(match).relative_to(copy_root).as_posix()
        for match in sorted(
            glob.glob(str(copy_root / "tests" / "draft_room" / "test_n03_*.py"))
        )
    )
    return files


def _failed_nodes(output: str) -> set:
    """Failing node ids from ``-rf`` short-summary lines (stdout or stderr)."""
    nodes = set()
    for line in output.splitlines():
        if not line.startswith("FAILED "):
            continue
        node = line[len("FAILED "):]
        nodes.add(node.split(" - ")[0].strip())
    return nodes


def _run_pytest(copy_root: Path, basetemp: Path) -> set:
    cmd = [sys.executable, "-m", "pytest"] + _testfiles(copy_root)
    cmd += PYTEST_ARGS + [f"--basetemp={basetemp.as_posix()}"]
    proc = subprocess.run(
        cmd,
        cwd=copy_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return _failed_nodes((proc.stdout or "") + "\n" + (proc.stderr or ""))


def _apply_mutation(pipeline_py: Path) -> None:
    """Delete the ``**payload,`` splat; refuse an ambiguous mutation site."""
    source = pipeline_py.read_text(encoding="utf-8")
    occurrences = source.count(MUTANT_LINE)
    if occurrences != 1:
        print(
            f"ABORT: expected the {MUTANT_LINE.strip()!r} line exactly once in "
            f"{pipeline_py}, found {occurrences}"
        )
        sys.exit(3)
    pipeline_py.write_text(
        source.replace(MUTANT_LINE, "", 1), encoding="utf-8"
    )


def main() -> int:
    tmp = _make_temp_root()
    try:
        clean_root = tmp / "backend-clean"
        mutated_root = tmp / "backend-mutated"
        shutil.copytree(REPO / "backend", clean_root, ignore=COPY_IGNORE)
        shutil.copytree(REPO / "backend", mutated_root, ignore=COPY_IGNORE)
        _apply_mutation(mutated_root / "app" / "services" / "draft_pipeline.py")

        clean_failures = _run_pytest(clean_root, tmp / "bt-clean")
        mutated_failures = _run_pytest(mutated_root, tmp / "bt-mutated")

        if clean_failures == mutated_failures:
            print("SURVIVED GX-026")
            return 1
        print("KILLED GX-026")
        for node in sorted(mutated_failures - clean_failures):
            print(f"  {node}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
