#!/usr/bin/env python3
"""Check setup-doc repo-truth contracts (issue #738).

Every mode compares a setup surface (.env.example, INSTALLATION.md,
docker-compose.yml, CONTRIBUTING.md) against repo truth (git-tracked files,
backend/app/config.py code defaults). A FAIL prints the exact offending line
and exits 1; an all-pass prints ``OK <mode>`` lines and exits 0.

Usage:
    check_g06_setup_docs.py --check {redis-url|cp-sources|allowed-extensions|
                                        data-dir|parsing-strategy|
                                        contributing-env-path|all}

The repo root resolves relative to this script's own location, so the gate
runs from any working directory.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

MODES = [
    "redis-url",
    "cp-sources",
    "allowed-extensions",
    "data-dir",
    "parsing-strategy",
    "contributing-env-path",
]

USAGE = (
    "usage: check_g06_setup_docs.py --check {"
    + "|".join(MODES)
    + "|all}"
)

# Fenced-block language tags treated as shell for cp-sources.
SHELL_FENCE_LANGS = {"sh", "bash", "shell", "zsh"}


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def active_env_entry(env_text: str, name: str) -> tuple[int, str] | None:
    """The ACTIVE (uncommented) ``NAME=value`` entry: (1-based line, value).

    Commented-out ``#NAME=...`` lines and absent keys both return None.
    """
    prefix = f"{name}="
    for line_no, line in enumerate(env_text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            return line_no, stripped[len(prefix):].strip()
    return None


def documented_env_entry(env_text: str, name: str) -> tuple[int, str] | None:
    """Any ACTIVE-or-commented ``NAME=`` entry: (1-based line, raw value).

    Modes that require a key to stay documented in the template (active or
    commented) use this; a key missing entirely fails those modes.
    """
    prefix = f"{name}="
    for line_no, line in enumerate(env_text.splitlines(), start=1):
        stripped = line.strip()
        candidate = stripped[1:].strip() if stripped.startswith("#") else stripped
        if candidate.startswith(prefix):
            return line_no, candidate[len(prefix):].strip()
    return None


def contiguous_comment_block_above(lines: list[str], line_no: int) -> str:
    """The contiguous '# ...' lines directly above the given 1-based line."""
    block: list[str] = []
    i = line_no - 2  # 0-based index of the line above line_no
    while i >= 0 and lines[i].strip().startswith("#"):
        block.append(lines[i])
        i -= 1
    return "\n".join(block)


def shell_fenced_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line, content) of every line inside a fenced shell block."""
    result: list[tuple[int, str]] = []
    in_fence = False
    fence_lang = ""
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                fence_lang = stripped.lstrip("`").strip()
                in_fence = True
            else:
                in_fence = False
                fence_lang = ""
            continue
        if in_fence and fence_lang in SHELL_FENCE_LANGS:
            result.append((line_no, line))
    return result


def git_tracked_files() -> set[str] | None:
    """Tracked paths from ``git ls-files`` (cwd=repo root), None on failure."""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"git ls-files failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0 or proc.stdout is None:
        print(f"git ls-files failed: {proc.stderr.strip()}", file=sys.stderr)
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# Modes — each returns a list of FAIL lines (empty list = pass).
# ---------------------------------------------------------------------------


def check_redis_url() -> list[str]:
    env_text = read(".env.example")
    if documented_env_entry(env_text, "REDIS_URL") is None:
        return [
            "FAIL redis-url: .env.example has no REDIS_URL entry (active or "
            "commented) - the key must stay documented"
        ]
    entry = active_env_entry(env_text, "REDIS_URL")
    if entry is None:
        return []
    line_no, value = entry
    host = (urlparse(value).hostname or "").lower()
    if host in ("localhost", "127.0.0.1"):
        return [f"FAIL redis-url: .env.example:{line_no} {value}"]
    return []


def check_cp_sources() -> list[str]:
    installation_text = read("INSTALLATION.md")
    tracked = git_tracked_files()
    if tracked is None:
        return ["FAIL cp-sources: git ls-files could not be run"]
    failures: list[str] = []
    for line_no, line in shell_fenced_lines(installation_text):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = re.match(r"cp\s+(\S+)\s+(\S+)", stripped)
        if not match:
            continue
        source = match.group(1)
        if source not in tracked:
            failures.append(f"FAIL cp-sources: INSTALLATION.md:{line_no} {source}")
    return failures


def parse_raster_image_extensions(artifacts_text: str) -> set[str] | None:
    match = re.search(
        r"RASTER_IMAGE_EXTENSIONS:\s*frozenset\[str\]\s*=\s*frozenset\(\s*\{(.*?)\}\s*\)",
        artifacts_text,
        re.DOTALL,
    )
    if not match:
        return None
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def check_allowed_extensions() -> list[str]:
    config_text = read("backend/app/config.py")
    literal = re.search(
        r"allowed_extensions:\s*(?:Annotated\[set\[str\],\s*NoDecode\]|set\[str\])"
        r"\s*=\s*\{(.*?)\}\s*\|\s*set\(RASTER_IMAGE_EXTENSIONS\)",
        config_text,
        re.DOTALL,
    )
    if not literal:
        return [
            "FAIL allowed-extensions: could not statically parse the "
            "allowed_extensions set literal in backend/app/config.py"
        ]
    artifacts_text = read("backend/app/services/document_artifacts.py")
    raster = parse_raster_image_extensions(artifacts_text)
    if raster is None:
        return [
            "FAIL allowed-extensions: could not statically parse "
            "RASTER_IMAGE_EXTENSIONS in "
            "backend/app/services/document_artifacts.py"
        ]
    code_extensions = set(re.findall(r'"([^"]+)"', literal.group(1))) | raster

    entry = active_env_entry(read(".env.example"), "ALLOWED_EXTENSIONS")
    if entry is None:
        return [
            "FAIL allowed-extensions: no active ALLOWED_EXTENSIONS= line in "
            ".env.example"
        ]
    _, value = entry
    template_extensions = {
        item.strip() for item in value.split(",") if item.strip()
    }
    return [
        f"FAIL allowed-extensions: missing {ext}"
        for ext in sorted(code_extensions - template_extensions)
    ] + [
        # The template line is ACTIVE: a value the code default does not
        # include silently widens the effective upload allowlist of every
        # deployment copying the template, so flag the reverse direction too.
        f"FAIL allowed-extensions: unexpected {ext}"
        for ext in sorted(template_extensions - code_extensions)
    ]


def check_data_dir() -> list[str]:
    env_text = read(".env.example")
    if documented_env_entry(env_text, "DATA_DIR") is None:
        return [
            "FAIL data-dir: .env.example has no DATA_DIR entry (active or "
            "commented) - the key must stay documented"
        ]
    entry = active_env_entry(env_text, "DATA_DIR")
    if entry is None:
        return []  # commented-out template key passes
    line_no, value = entry
    compose_text = read("docker-compose.yml")
    compose_entry = re.search(
        r"^\s*-\s*DATA_DIR=(.+)$", compose_text, re.MULTILINE
    )
    if not compose_entry:
        return [
            "FAIL data-dir: docker-compose.yml environment list has no "
            "DATA_DIR entry but .env.example has an active one"
        ]
    if value != compose_entry.group(1).strip():
        return [f"FAIL data-dir: .env.example:{line_no} {value}"]
    return []


def check_parsing_strategy() -> list[str]:
    env_text = read(".env.example")
    if documented_env_entry(env_text, "DOCUMENT_PARSING_STRATEGY") is None:
        return [
            "FAIL parsing-strategy: .env.example has no DOCUMENT_PARSING_STRATEGY "
            "entry (active or commented) - the key must stay documented so "
            "operators overriding parsing behavior can discover it; an invalid "
            "value (not an absent one) fails startup validation"
        ]
    entry = active_env_entry(env_text, "DOCUMENT_PARSING_STRATEGY")
    if entry is None:
        return []  # commented-out template key passes
    line_no, value = entry
    config_text = read("backend/app/config.py")
    default = re.search(
        r'document_parsing_strategy:\s*str\s*=\s*"([^"]*)"', config_text
    )
    if not default:
        return [
            "FAIL parsing-strategy: could not statically parse "
            'document_parsing_strategy default in backend/app/config.py'
        ]
    code_default = default.group(1)
    if value == code_default:
        return []
    comment_block = contiguous_comment_block_above(
        env_text.splitlines(), line_no
    )
    documents_default = (
        "code default" in comment_block.lower()
        and code_default in comment_block
    )
    if documents_default:
        return []
    return [f"FAIL parsing-strategy: .env.example:{line_no} {value}"]


def check_contributing_env_path() -> list[str]:
    contributing_text = read("CONTRIBUTING.md")
    lines = contributing_text.splitlines()
    found_paragraph = False
    for index, line in enumerate(lines):
        if "**Environment:**" not in line:
            continue
        found_paragraph = True
        paragraph: list[str] = []
        cursor = index
        while cursor < len(lines) and lines[cursor].strip():
            paragraph.append(lines[cursor])
            cursor += 1
        paragraph_text = "\n".join(paragraph)
        if ".env.example" in paragraph_text and "backend/.env" not in paragraph_text:
            return ["FAIL contributing-env-path"]
        if "backend/.env" not in paragraph_text:
            return [
                "FAIL contributing-env-path: **Environment:** paragraph must "
                "name backend/.env - the native-run env-file step must stay "
                "documented"
            ]
    if not found_paragraph:
        return [
            "FAIL contributing-env-path: CONTRIBUTING.md has no **Environment:** "
            "paragraph - the native-run env-file step must stay documented"
        ]
    return []


CHECKS = {
    "redis-url": check_redis_url,
    "cp-sources": check_cp_sources,
    "allowed-extensions": check_allowed_extensions,
    "data-dir": check_data_dir,
    "parsing-strategy": check_parsing_strategy,
    "contributing-env-path": check_contributing_env_path,
}


def run_mode(mode: str) -> int:
    failures = CHECKS[mode]()
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print(f"OK {mode}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 2 or args[0] != "--check":
        print(USAGE, file=sys.stderr)
        return 2
    mode = args[1]
    if mode == "all":
        for name in MODES:
            exit_code = run_mode(name)
            if exit_code != 0:
                return exit_code
        return 0
    if mode not in MODES:
        print(USAGE, file=sys.stderr)
        return 2
    return run_mode(mode)


if __name__ == "__main__":
    raise SystemExit(main())
