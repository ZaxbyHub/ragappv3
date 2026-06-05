#!/usr/bin/env python3
"""copilot-pr-publication-gate.py

Best-effort preToolUse guardrail for AI coding agents. It blocks
`gh pr create|new|edit|ready` until the PR body satisfies the ragappv3
commit-pr publication contract, nudging the agent back to the single source of
truth before it publishes.

This is a GUARDRAIL, not authoritative enforcement. Authoritative enforcement
is the CI `quality-contracts` job plus branch protection on `master`. Whether
the GitHub Copilot cloud agent invokes repository hooks under `.github/hooks/`
is not documented as a supported feature, so do not rely on this hook alone --
it is the first line of defense.

The publication contract is owned by `.claude/skills/commit-pr/SKILL.md`.
Unlike the opencode-swarm original, ragappv3 has no `.swarm/evidence/`
convention, so this gate inspects the actual `--body` / `--body-file` argument
of the `gh pr` command in the hook payload and requires the PR-body sections
that the commit-pr skill mandates: `## Summary` and `## Test plan`.

Input: the hook payload (the tool invocation, including the command the agent
is about to run) is read from stdin.
"""

import re
import shlex
import sys
from pathlib import Path

# PR-body sections required by .claude/skills/commit-pr/SKILL.md.
REQUIRED_SECTIONS = ("## Summary", "## Test plan")

# Only gate commands that publish or update a pull request. `new` is a common
# alias of `create`. Token boundaries avoid false positives like
# "enough pr edits remain".
PR_PUBLISH_RE = re.compile(
    r"(^|[^A-Za-z0-9])gh\s+pr\s+(create|new|edit|ready)([^A-Za-z0-9]|$)",
    re.IGNORECASE,
)


def _block(message: str) -> None:
    print(message)
    print(
        "Load .claude/skills/commit-pr/SKILL.md (the single source of truth) "
        "and satisfy its checklist before publishing."
    )
    sys.exit(1)


def _extract_body(payload: str) -> "str | None":
    """Return the PR body text from the gh command, or None if not found."""
    try:
        tokens = shlex.split(payload)
    except ValueError:
        # Unparseable payload (unbalanced quotes etc.) -- fall back to raw text.
        tokens = payload.split()

    body_flags = {"--body", "-b"}
    file_flags = {"--body-file", "-F"}

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # `--body=...` / `--body-file=...` inline forms.
        if "=" in tok:
            key, _, val = tok.partition("=")
            if key in body_flags:
                return val
            if key in file_flags:
                return _read_body_file(val)
        elif tok in body_flags and i + 1 < len(tokens):
            return tokens[i + 1]
        elif tok in file_flags and i + 1 < len(tokens):
            return _read_body_file(tokens[i + 1])
        i += 1
    return None


def _read_body_file(path: str) -> "str | None":
    if path == "-":  # body from stdin -- not inspectable here.
        return None
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def main() -> int:
    payload = sys.stdin.read() if not sys.stdin.isatty() else ""

    # Only gate pull-request publish/update commands.
    if not PR_PUBLISH_RE.search(payload):
        return 0

    # `gh pr ready` flips draft state and carries no body -- allow it; the body
    # was already gated at create/edit time.
    if re.search(r"(^|[^A-Za-z0-9])gh\s+pr\s+ready", payload, re.IGNORECASE):
        return 0

    body = _extract_body(payload)
    if body is None:
        _block(
            "Blocked: could not find a PR body (--body or --body-file) on this "
            "gh pr command. Write the exact PR body you intend to publish, "
            "including the required sections."
        )

    missing = [s for s in REQUIRED_SECTIONS if s not in body]
    if missing:
        _block(
            "Blocked: PR body is missing required section(s): "
            + ", ".join(missing)
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
