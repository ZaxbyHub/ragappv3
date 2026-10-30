#!/usr/bin/env python3
"""Shared, hardened git plumbing for the Quality-contracts gate scripts.

Issue #740 distilled this from ``check_test_collection_scope._git_paths``
(the reference implementation written in issue #656's wake): every older gate
script re-implemented its own ``git`` invocation, so the hardened idioms
never propagated — unbounded hangs, tracebacks when git is missing, and
C-quoted paths under git's default ``core.quotePath`` all shipped because
nothing tested them.

Contract:

* Listing verbs return NUL-delimited raw tokens (``-z`` injected): git's
  default ``core.quotePath`` C-quotes non-ASCII paths, and a quoted path can
  never match a glob or prefix built from the real name.
* Every call is bounded by ``GIT_TIMEOUT_SECONDS``: a hung git fails the
  gate with a message instead of stalling the job until its timeout.
* ``OSError`` (binary missing), ``subprocess.TimeoutExpired`` and decode
  misconfiguration (``ValueError``) degrade to ``None`` — the CALLER decides
  whether that is a skip-with-note or a failure. Output decodes with
  ``errors="surrogateescape"`` so odd-but-real path bytes round-trip.
* Content verbs (``git show <rev>:<path>``) are NOT path listings: they
  return the raw text (or ``None``), still bounded and OSError-tolerant.

Import (dual-context safe — works under ``python scripts/check_x.py`` AND
under test loaders that import the gate script via spec_from_file_location
without putting ``scripts/`` on ``sys.path``)::

    import importlib.util as _ilu
    from pathlib import Path as _Path
    _spec = _ilu.spec_from_file_location(
        "_gate_git", _Path(__file__).resolve().parent / "_gate_git.py")
    _gate_git = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_gate_git)

Stdlib-only on purpose (the Quality contracts job runs scripts/check_*.py
under a bare setup-python with no dependency install).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 60

# Verbs whose stdout is a path listing (NUL-delimited with -z). Anything else
# is treated as a content/status call and returned as text. `status` is
# deliberately NOT a listing verb here: `git status --porcelain -z` records
# carry the XY status prefix, not raw repo-relative paths, and the token
# shape differs from listings (PR #1045 review, PRR-016) — status reads go
# through run_git and parse their own record shape.
_LISTING_VERBS = {
    "ls-files",
    "diff",
}


def run_git(
    root: Path,
    *args: str,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str] | None:
    """Run git under the hardening contract; None when git cannot answer."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def git_lines(root: Path, *args: str) -> list[str] | None:
    """Run a LISTING git command; return raw repo-relative tokens or None.

    ``-z`` is injected right after the verb (``git ls-files -z``,
    ``git diff --name-only -z`` ...) and the output splits on NUL with only
    the empty terminal token discarded — tokens keep their raw bytes, so a
    non-ASCII path arrives unquoted and matches real globs/prefixes.
    """
    if args:
        verb = args[0]
        rest: tuple[str, ...] = ()
        if verb == "diff":
            # -z is a global flag for diff and belongs before subcommand
            # flags like --name-only.
            rest = tuple(args[1:])
            call_args: tuple[str, ...] = ("diff", "-z", *rest)
        else:
            rest = tuple(args[1:])
            call_args = (verb, *rest, "-z")
        if verb in _LISTING_VERBS:
            proc = run_git(root, *call_args)
            if proc is None or proc.returncode != 0 or proc.stdout is None:
                return None
            return [token for token in proc.stdout.split("\0") if token]
    proc = run_git(root, *args)
    if proc is None or proc.returncode != 0 or proc.stdout is None:
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]
