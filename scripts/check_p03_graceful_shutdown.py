#!/usr/bin/env python3
"""Check the container graceful-shutdown envelope (issue #799 AC5).

Reads the backend uvicorn ``CMD`` in the Dockerfile, the uvicorn argument
list in start-services.ps1, and the ``stop_grace_period`` in
docker-compose.yml, and enforces the shutdown budget on all three surfaces:

1. the Dockerfile CMD runs uvicorn with a bounded
   ``--timeout-graceful-shutdown <int>`` (space or ``=`` form, INTEGER ONLY
   — uvicorn 0.54 rejects a unit suffix such as ``75s`` at startup), so
   uvicorn stops waiting on open connections (e.g. SSE streams) and hands
   control back to the lifespan shutdown sequence within a bounded window;
2. start-services.ps1 starts uvicorn with the SAME integer flag, so the
   Windows dev stack shares the container's shutdown bound;
3. docker-compose.yml sets a ``stop_grace_period:`` STRICTLY longer than the
   uvicorn grace seconds, so Docker never SIGKILLs the process before
   uvicorn's own graceful window has closed.

Exit 0 with ``OK`` lines when all hold; exit 1 after printing the first
``FAIL:`` line otherwise. The repo root resolves relative to this script's
own location, so the gate runs from any working directory.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The flag token itself (value captured separately so a unit-suffixed value
# like "75s" can be rejected distinctly from a missing flag).
FLAG_TOKEN_RE = re.compile(r"--timeout-graceful-shutdown(?![\w-])")
FLAG_VALUE_RE = re.compile(r"--timeout-graceful-shutdown(?:\s+|=)([^\s\"'\]]+)")
GRACE_RE = re.compile(r"^\s*stop_grace_period:\s*(\S+)\s*$", re.MULTILINE)

_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _parse_flag_seconds(line: str, *, missing_message: str) -> int | None:
    """Integer seconds from the flag on ``line``, or None after failing.

    ``missing_message`` is printed (exit-code handled by the caller's
    contract: this helper prints and returns None) when the flag is absent;
    a present-but-non-integer value prints the shared must-be-integer FAIL.
    """
    if not FLAG_TOKEN_RE.search(line):
        print(missing_message)
        return None
    value_match = FLAG_VALUE_RE.search(line)
    if value_match is None or not re.fullmatch(r"\d+", value_match.group(1)):
        print(
            "FAIL: Dockerfile uvicorn --timeout-graceful-shutdown must be "
            "an integer number of seconds"
        )
        return None
    return int(value_match.group(1))


def _ps1_logical_lines(text: str) -> list[str]:
    """Join backtick line continuations into single logical lines."""
    return [
        logical.replace("`", " ")
        for logical in text.replace("`\n", " ").splitlines()
    ]


def _token_soup(region: str) -> str:
    """Normalize a PowerShell launch region into a flat token soup:
    backtick continuations, commas, double quotes, ``@(`, ``)`` and
    whitespace all collapse to single spaces, so both the current
    single-string ArgumentList form and the planned array+wrapper form
    (``$BackendArgs = @(...)`` splatted into ``-ArgumentList``) reduce to
    the same searchable text."""
    return re.sub(r'[,@()"`\s]+', " ", region)


def _ps1_backend_launch_soup(path: Path | None = None) -> str | None:
    """Token soup of the backend uvicorn launch region of
    start-services.ps1, or None when the script has no uvicorn launch.

    Two shapes are recognized:
    - current base form: one ``-ArgumentList`` logical line carrying the
      quoted tokens ``"-m","uvicorn",...`` directly;
    - planned form (05-fix-plan.md rev 4): a PowerShell array variable
      (``$BackendArgs = @("-m","uvicorn",...)``) plus the wrapper
      Start-Process lines that pass it through
      (``-ArgumentList (@("-c",$wrapper)+$BackendArgs)``).
    """
    ps1_path = path if path is not None else ROOT / "start-services.ps1"
    logical = _ps1_logical_lines(ps1_path.read_text(encoding="utf-8"))

    def _has_uvicorn_tokens(line: str) -> bool:
        tokens = re.findall(r'"([^"]*)"', line)
        return "-m" in tokens and "uvicorn" in tokens

    for line in logical:
        if "-ArgumentList" in line and "uvicorn" in line:
            if _has_uvicorn_tokens(line):
                return _token_soup(line)
    for index, line in enumerate(logical):
        if "uvicorn" not in line or "$" not in line:
            continue
        if not re.search(r"^\s*\$\w+\s*=\s*@\(", line):
            continue
        if _has_uvicorn_tokens(line):
            region = [line] + logical[index + 1 : index + 6]
            return _token_soup(" ".join(region))
    return None


def _parse_compose_seconds(value: str) -> float | None:
    """Compose duration (``90s``, ``1m30s``) to seconds; unitless = seconds.

    Returns None when the value contains no numeric component at all.
    """
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)([smhd]?)", value):
        total += float(amount) * _UNIT_SECONDS.get(unit or "s", 1.0)
        matched = True
    return total if matched else None


def main() -> int:
    dockerfile_text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd_lines = [
        line
        for line in dockerfile_text.splitlines()
        if line.lstrip().startswith("CMD")
    ]
    if not cmd_lines:
        print("FAIL: Dockerfile has no CMD line")
        return 1

    flag_line = next(
        (line for line in cmd_lines if FLAG_TOKEN_RE.search(line)), None
    )
    if flag_line is None:
        print("FAIL: Dockerfile uvicorn CMD has no --timeout-graceful-shutdown")
        return 1

    uvicorn_grace_seconds = _parse_flag_seconds(
        flag_line,
        missing_message=(
            "FAIL: Dockerfile uvicorn CMD has no --timeout-graceful-shutdown"
        ),
    )
    if uvicorn_grace_seconds is None:
        return 1
    print(
        "OK Dockerfile CMD bounds uvicorn --timeout-graceful-shutdown "
        f"at {uvicorn_grace_seconds}s"
    )

    ps1_soup = _ps1_backend_launch_soup()
    if ps1_soup is None:
        print("FAIL: start-services.ps1 has no uvicorn -ArgumentList")
        return 1
    if not FLAG_TOKEN_RE.search(ps1_soup):
        print("FAIL: start-services.ps1 uvicorn args have no --timeout-graceful-shutdown")
        return 1
    ps1_value_match = FLAG_VALUE_RE.search(ps1_soup)
    if ps1_value_match is None or not re.fullmatch(
        r"\d+", ps1_value_match.group(1)
    ):
        print(
            "FAIL: start-services.ps1 uvicorn --timeout-graceful-shutdown "
            "must be an integer number of seconds"
        )
        return 1
    ps1_seconds = int(ps1_value_match.group(1))
    if ps1_seconds != uvicorn_grace_seconds:
        print(
            "FAIL: start-services.ps1 uvicorn --timeout-graceful-shutdown "
            f"({ps1_seconds}s) must match the Dockerfile CMD value "
            f"({uvicorn_grace_seconds}s)"
        )
        return 1
    print(
        "OK start-services.ps1 uvicorn args carry the same "
        f"--timeout-graceful-shutdown {uvicorn_grace_seconds}s"
    )

    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    grace_match = GRACE_RE.search(compose_text)
    if grace_match is None:
        print("FAIL: docker-compose.yml has no stop_grace_period")
        return 1
    grace_value = grace_match.group(1)
    compose_grace_seconds = _parse_compose_seconds(grace_value)
    if compose_grace_seconds is None:
        print(
            f"FAIL: cannot parse docker-compose.yml stop_grace_period value "
            f"{grace_value!r} as a duration"
        )
        return 1
    if not compose_grace_seconds > uvicorn_grace_seconds:
        print(
            f"FAIL: docker-compose stop_grace_period ({grace_value} = "
            f"{compose_grace_seconds:g}s) must be strictly greater than the "
            f"uvicorn --timeout-graceful-shutdown ({uvicorn_grace_seconds}s)"
        )
        return 1
    print(
        f"OK docker-compose.yml stop_grace_period {grace_value} is strictly "
        f"greater than the uvicorn grace {uvicorn_grace_seconds}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
