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

1. Refuse to certify from a dirty ``backend/`` tree (an untracked scratch
   ``test_n03_*.py`` would silently join the certified surface and can supply
   a mutant-only failure); ``--allow-dirty`` overrides, for a deliberate
   local experiment.
2. Copy ``backend/`` into two throwaway trees (clean and mutated), excluding
   caches, virtualenvs AND gitignored local state (``.env``, ``*.db``,
   ``data/``, coverage files, ``.tmp-*`` scratch dirs) so no developer data or
   secret is relocated into a scratch directory.
3. In the mutated copy only, delete the single ``**payload,`` line (aborting
   if it does not occur exactly once, so a refactor cannot silently neuter
   the mutation).
4. Run the frozen acceptance surface — ``test_draft_pipeline.py`` plus every
   ``test_n03_*.py`` check file discovered INSIDE each copy, so a post-fix
   check file is picked up automatically — in both trees, with a per-run
   timeout and a pytest timeout.
5. Compare the failing node-id sets over BOTH failures and errors. A clean
   run that is not fully green (failure, error, or non-zero exit) aborts the
   certification (exit 3) rather than being mistaken for a surviving mutant;
   any mutant-only failing or erroring node means KILLED (exit 0); identical
   clean sets mean SURVIVED (exit 1).

Run from the repo root::

    python scripts/check_n03_gx026_mutant.py

Exit codes: 0 = KILLED, 1 = SURVIVED, 3 = the certification could not be
run reliably (dirty tree without ``--allow-dirty``, mutation site not found
exactly once, or the clean run was not green).

The kill is certified under a deterministic order (``-p no:randomly``); CI
runs the same tests randomized, which is a different regime — set
``N03_PYTEST_ARGS`` to override the pytest arguments if you want to certify
under CI's exact flags.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The exact mutated line: the ``**payload,`` splat in ``_stage_input_hash``.
MUTANT_LINE = "        **payload,\n"

#: Copied trees exclude caches, virtualenvs AND gitignored local state: the
#: latter would otherwise relocate developer data (SQLite databases, vault
#: uploads, ``.env``) into a scratch directory for no certification value.
COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".venv*",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data",
    ".env",
    ".env.*",
    "*.db",
    "*.db-*",
    "*.sqlite*",
    ".coverage",
    ".coverage.*",
    ".tmp-*",
)

_DEFAULT_PYTEST_ARGS = [
    "-q",
    "-p", "no:randomly",
    "-p", "no:cacheprovider",
    # -rA prints BOTH failures and errors in the short summary; -rf alone
    # would hide an errored/collection-failed run behind an empty set.
    "-rA",
    "--timeout=900",
]

#: Wall-clock ceiling per pytest run, so a hanging mutant fails the run
#: instead of hanging the instrument forever (CI passes --timeout too).
_SUBPROCESS_TIMEOUT = 1800


def _pytest_args() -> list:
    override = os.environ.get("N03_PYTEST_ARGS")
    if override:
        return override.split()
    return list(_DEFAULT_PYTEST_ARGS)


def _assert_clean_tree() -> None:
    """Refuse to certify from a dirty backend/ (see module docstring)."""
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", str(REPO / "backend")],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()
    if dirty:
        print(
            "ABORT: backend/ has uncommitted changes:\n"
            + dirty
            + "\nCertify from a clean tree (or pass --allow-dirty for a "
            "deliberate local experiment).",
            file=sys.stderr,
        )
        raise SystemExit(3)


def _make_temp_root() -> Path:
    """A writable scratch root: the portable system temp dir, then .swarm/.

    Hardcoded drive letters (``E:/tmp``) are relative paths on POSIX, where
    ``mkdir`` happily creates a junk ``E:/`` tree inside the repository.
    """
    candidates = [tempfile.gettempdir(), str(REPO / ".swarm")]
    for candidate in candidates:
        try:
            Path(candidate).mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="n03-gx026-", dir=candidate))
        except OSError:
            continue
    raise SystemExit("ABORT: no writable scratch root among " + ", ".join(candidates))


def _testfiles(copy_root: Path) -> list:
    """The frozen acceptance surface, discovered inside one copied tree.

    ``test_n03_*.py`` files are globbed relative to the copy so a check file
    added by the fix is picked up without editing this script.
    """
    files = ["tests/draft_room/test_draft_pipeline.py"]
    files.extend(
        Path(match).relative_to(copy_root).as_posix()
        for match in sorted(
            glob.glob(str(copy_root / "tests" / "draft_room" / "test_n03_*.py"))
        )
    )
    return files


def _outcome_nodes(output: str) -> set:
    """Node ids that FAILED or ERRORED, from the ``-rA`` short summary."""
    nodes = set()
    for line in output.splitlines():
        for prefix in ("FAILED ", "ERROR "):
            if line.startswith(prefix):
                nodes.add(line[len(prefix):].split(" - ")[0].strip())
    return nodes


def _run_pytest(copy_root: Path, basetemp: Path):
    """Run the surface in one copy; return (exit status, failing node ids)."""
    cmd = (
        [sys.executable, "-m", "pytest"]
        + _testfiles(copy_root)
        + _pytest_args()
        + [f"--basetemp={basetemp.as_posix()}"]
    )
    proc = subprocess.run(
        cmd,
        cwd=str(copy_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_SUBPROCESS_TIMEOUT,
    )
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode, _outcome_nodes(output)


def _run_pytest_guarded(copy_root: Path, basetemp: Path):
    """``_run_pytest`` that turns a hung suite into an ABORT, never a verdict.

    An uncaught TimeoutExpired would exit 1 - the documented SURVIVED code -
    which is exactly the false-survival channel the exit contract exists to
    close.
    """
    try:
        return _run_pytest(copy_root, basetemp)
    except subprocess.TimeoutExpired:
        print(
            f"ABORT: the pytest run in {copy_root.name} exceeded "
            f"{_SUBPROCESS_TIMEOUT}s and was killed",
            file=sys.stderr,
        )
        raise SystemExit(3)


def _apply_mutation(pipeline_py: Path) -> None:
    """Delete the ``**payload,`` splat; refuse an ambiguous mutation site."""
    source = pipeline_py.read_text(encoding="utf-8")
    occurrences = source.count(MUTANT_LINE)
    if occurrences != 1:
        print(
            f"ABORT: expected the {MUTANT_LINE.strip()!r} line exactly once in "
            f"{pipeline_py}, found {occurrences}",
            file=sys.stderr,
        )
        sys.exit(3)
    pipeline_py.write_text(
        source.replace(MUTANT_LINE, "", 1), encoding="utf-8", newline=""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="certify even though backend/ has uncommitted changes",
    )
    args = parser.parse_args()
    if not args.allow_dirty:
        _assert_clean_tree()

    tmp = _make_temp_root()
    try:
        clean_root = tmp / "backend-clean"
        mutated_root = tmp / "backend-mutated"
        print(f"scratch root: {tmp}")
        shutil.copytree(REPO / "backend", clean_root, ignore=COPY_IGNORE)
        shutil.copytree(REPO / "backend", mutated_root, ignore=COPY_IGNORE)
        _apply_mutation(mutated_root / "app" / "services" / "draft_pipeline.py")

        clean_status, clean_failed = _run_pytest_guarded(clean_root, tmp / "bt-clean")
        if clean_status != 0 or clean_failed:
            print(
                "ABORT: the CLEAN run was not green (exit "
                f"{clean_status}, {len(clean_failed)} failing/erroring "
                "nodes) - the acceptance surface itself is broken, which is "
                "not a surviving mutant",
                file=sys.stderr,
            )
            for node in sorted(clean_failed)[:10]:
                print(f"  {node}", file=sys.stderr)
            return 3
        print(f"  clean run: green ({len(clean_failed)} failing nodes)")

        mutated_status, mutated_failed = _run_pytest_guarded(
            mutated_root, tmp / "bt-mutated"
        )
        print(
            f"  mutated run: exit {mutated_status}, "
            f"{len(mutated_failed)} failing/erroring nodes"
        )

        only_mutant = mutated_failed - clean_failed
        if not only_mutant and mutated_status == clean_status:
            print("SURVIVED GX-026")
            return 1
        print("KILLED GX-026")
        for node in sorted(only_mutant):
            print(f"  {node}")
        if not only_mutant:
            print("  (no mutant-only node ids; killed by exit status alone)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
