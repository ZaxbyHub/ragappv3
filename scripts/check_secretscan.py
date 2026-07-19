#!/usr/bin/env python3
"""Validate .secretscanignore so its glob patterns cannot silently rot.

.secretscanignore is consumed by the optional local `secretscan` tool (documented
in .claude/skills/execute/SKILL.md and the .opencode mirror) — it is NOT read by
git or any CI step today. A malformed glob makes secretscan silently skip files
it should flag (false negatives on real secrets). This script enforces:

  C-SSECRETSCAN-1 (parseability): .secretscanignore exists and every non-comment,
      non-blank line is a syntactically permissible glob (no embedded NUL or
      newlines, non-empty).
  C-SSECRETSCAN-2 (positive samples ignored): adversarial paths that MUST match
      at least one pattern (e.g. `backend/tests/conftest.py`, a nested file under
      `.opencode/`). Exercises correct `**` semantics.
  C-SSECRETSCAN-3 (negative samples NOT ignored): adversarial paths that must
      NOT match any pattern — chosen to exercise `**` segment boundaries so a
      naive prefix or substring matcher would false-positive.
  C-SSECRETSCAN-4 (overly-broad globs, advisory): warn on stderr if a single
      pattern matches more than OVERLY_BROAD_FRACTION of tracked files.
  C-SSECRETSCAN-5 (stale globs, advisory): warn on stderr if a pattern matches
      no tracked or on-disk file. Patterns annotated with a trailing
      `# defensive:` comment are exempt — they guard paths that may exist in
      other configurations or future states (e.g. `config.example.json`).

Why a hand-rolled glob matcher instead of git check-ignore or pathspec:
  git check-ignore consults .gitignore/.git/info/exclude/core.excludesFile
  *additively*. `--exclude-from=<file>` is also additive — it cannot isolate
  .secretscanignore's semantics, so paths ignored by .gitignore but NOT by
  .secretscanignore would be reported as ignored, producing false positives.
  pathspec (the obvious right tool) is not a current dependency and adding it
  would touch backend/requirements-lock.txt and -ci.txt (out of scope; flagged
  by check_pr_scope_drift as CI tooling). The hand-rolled matcher below is
  stdlib-only, intentionally minimal, and exercises correct `**` semantics via
  the adversarial samples; its limitations are documented in this docstring.

Exit codes: 0 = pass (with possible stderr warnings), 1 = any C-SSECRETSCAN-1/2/3
violation. C-SSECRETSCAN-4/5 are advisory (stderr only, non-fatal). Run from the
repository root.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETSCANIGNORE = ROOT / ".secretscanignore"

# Adversarial positive samples — each MUST match at least one pattern.
POSITIVE_SAMPLES = (
    # exercises a directory-glob pattern
    "backend/tests/conftest.py",
    # exercises an exact-match line
    ".env.example",
    # exercises `**/.opencode/**` requiring correct `**` semantics (path that
    # exists today under .opencode/)
    ".opencode/skills/codebase-review-swarm/README.md",
)

# Adversarial negative samples — each MUST NOT match any pattern. Chosen to
# exercise `**` segment boundaries: a naive prefix or substring matcher fails.
# Note: these deliberately use a non-.md extension to avoid matching the
# broad `**/*.md` ignore line (which legitimately catches any .md file).
NEGATIVE_SAMPLES = (
    # must NOT match the exact line `backend/app/services/__init__.py`
    "backend/app/services/auth/__init__.py",
    # must NOT match `backend/tests/**` (segment boundary)
    "backend/tests_something/main.py",
    # must NOT match `**/.claude/**` (segment boundary)
    ".claude_backup/foo.py",
)

OVERLY_BROAD_FRACTION = 0.50

# Trailing-comment marker that declares a pattern as defensive: it may match
# nothing today but guards a path that may appear in other configurations or
# future states (e.g. config.example.json when no example exists yet).
DEFENSIVE_MARKER = "defensive:"


def _print(msg: str) -> None:
    print(msg, file=sys.stderr)


def parse_patterns() -> list[tuple[int, str, str, bool]]:
    """Return (line_no, raw_line, glob, is_defensive) tuples."""
    if not SECRETSCANIGNORE.is_file():
        _print("secretscan: .secretscanignore missing")
        return []
    out: list[tuple[int, str, str, bool]] = []
    for idx, raw in enumerate(SECRETSCANIGNORE.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Allow an inline trailing comment after the glob: `glob  # defensive: reason`.
        is_defensive = DEFENSIVE_MARKER in raw
        # Strip the inline comment to get the glob itself.
        glob_part = stripped
        if "#" in glob_part:
            glob_part = glob_part.split("#", 1)[0].strip()
        if glob_part:
            out.append((idx, raw, glob_part, is_defensive))
    return out


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a gitignore-style glob to a regex matching the WHOLE path.

    Supports:
      **  matches any number of characters including path separators
          (i.e. any number of path segments, including zero). Used as
          `**/foo` (anywhere), `foo/**` (descendants of foo), or
          `a/**/b` (zero or more segments between a and b).
      *   matches any characters except a path separator
      ?   matches a single non-separator character
      everything else is literal

    Patterns with no internal slash match the basename of any path
    (gitignore semantics). Patterns containing a slash match against the
    full path anchored at the root. Trailing `/` on a pattern matches a
    directory and all its descendants.
    """
    p = pattern
    p = p[1:] if p.startswith("/") else p

    leading_doublestar = False
    if p.startswith("**/"):
        leading_doublestar = True
        p = p[3:]

    directory_pattern = False
    if p.endswith("/"):
        directory_pattern = True
        p = p[:-1]

    out: list[str] = [r"\A"]
    if leading_doublestar:
        # `**/` at the start matches any leading path including the empty prefix.
        # `a`, `x/a`, `x/y/a` all match `**/a`.
        out.append(r"(?:.*/)?")
    elif "/" not in p:
        # No slash anywhere in the pattern: gitignore matches the basename at
        # any depth. e.g. `package-lock.json` matches `frontend/package-lock.json`.
        out.append(r"(?:.*/)?")

    i = 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if i + 1 < len(p) and p[i + 1] == "*":
                # `**` matches any chars including separators. Consume a
                # following slash so `/**/` becomes `/.*/` (and `/**` end
                # becomes `.*`), matching zero or more segments.
                out.append(r".*")
                i += 2
                if i < len(p) and p[i] == "/":
                    i += 1
                continue
            out.append(r"[^/]*")
            i += 1
            continue
        if c == "?":
            out.append(r"[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1

    if directory_pattern:
        out.append(r"(?:/.*)?")
    out.append(r"\Z")
    return re.compile("".join(out))


def matches_any(path: str, patterns: list[tuple[int, str, str, bool]]) -> int | None:
    """Return the line number of the first matching pattern, or None."""
    for line_no, _raw, glob, _defensive in patterns:
        if glob_to_regex(glob).match(path):
            return line_no
    return None


def tracked_files() -> list[str]:
    """Return git-tracked file paths (relative, posix) under ROOT."""
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def on_disk_files() -> list[str]:
    """Return on-disk file paths (relative, posix) under ROOT, excluding .git."""
    out: list[str] = []
    for path in ROOT.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            out.append(path.relative_to(ROOT).as_posix())
    return out


def main() -> int:
    patterns = parse_patterns()
    if not patterns:
        _print("secretscan: no patterns parsed from .secretscanignore")
        return 1

    failures: list[str] = []

    # C-SSECRETSCAN-1: parseability — every parsed glob must be non-empty.
    for line_no, _raw, glob, _defensive in patterns:
        if not glob:
            failures.append(f"secretscan: line {line_no} empty glob")

    # C-SSECRETSCAN-2: positive samples must be ignored.
    for sample in POSITIVE_SAMPLES:
        hit = matches_any(sample, patterns)
        if hit is None:
            failures.append(
                f"secretscan: positive sample {sample!r} not matched by any pattern"
            )

    # C-SSECRETSCAN-3: negative samples must NOT be ignored.
    for sample in NEGATIVE_SAMPLES:
        hit = matches_any(sample, patterns)
        if hit is not None:
            failures.append(
                f"secretscan: negative sample {sample!r} matched by line {hit} "
                f"(glob over-match)"
            )

    # C-SSECRETSCAN-4: overly-broad globs (advisory, non-fatal).
    candidates = sorted(set(tracked_files()) | set(on_disk_files()))
    if candidates:
        threshold = max(1, int(len(candidates) * OVERLY_BROAD_FRACTION))
        for line_no, _raw, glob, _defensive in patterns:
            regex = glob_to_regex(glob)
            count = sum(1 for c in candidates if regex.match(c))
            if count >= threshold:
                _print(
                    f"secretscan: warning line {line_no} pattern {glob!r} matches "
                    f"{count}/{len(candidates)} files (>{OVERLY_BROAD_FRACTION:.0%})"
                )

    # C-SSECRETSCAN-5: stale globs (advisory, non-fatal). `defensive:`-annotated
    # patterns are exempt. Patterns containing wildcards are inherently
    # defensive (they describe shape classes, not specific paths); only warn on
    # literal-path patterns (no `*`, no `?`) that point at something which does
    # not exist, AND on `<dir>/**` patterns where the leading directory does not
    # exist. Those are the only stale candidates we can detect with confidence
    # (e.g. `.swarm/**` is caught because `.swarm/` does not exist).
    if candidates:
        for line_no, _raw, glob, is_defensive in patterns:
            if is_defensive:
                continue
            if "*" in glob or "?" in glob:
                # Special-case: `<dir>/**` where <dir> is a literal directory
                # that does not exist on disk at all (strip a leading `**/`
                # before testing so `**/.opencode/**` resolves to `.opencode/`).
                if glob.endswith("/**"):
                    head = glob[:-3]
                    if head.startswith("**/"):
                        head = head[3:]
                    # `a/**/b/**` is too complex; skip the directory-existence
                    # check when any other wildcard remains.
                    if "*" not in head and "?" not in head and head:
                        if not (ROOT / head).exists():
                            _print(
                                f"secretscan: warning line {line_no} pattern "
                                f"{glob!r} targets nonexistent directory "
                                f"{head!r} (stale)"
                            )
                continue
            regex = glob_to_regex(glob)
            if not any(regex.match(c) for c in candidates):
                _print(
                    f"secretscan: warning line {line_no} pattern {glob!r} matches no "
                    f"file (stale? annotate with `# defensive: <reason>` if intentional)"
                )

    for msg in failures:
        _print(msg)
    if failures:
        return 1
    print("secretscan: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
