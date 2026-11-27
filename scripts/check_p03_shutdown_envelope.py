#!/usr/bin/env python3
"""Check the process-shutdown envelope's ONE derivation (#799 AC5 / #854).

The natural worst case of a graceful shutdown is a SUM of independently
enforced windows. When that total is written by hand in several places
(docker-compose.yml, both .ps1 launchers, the lifespan module, the
release fragment), the copies drift and the force-kill boundary can end up
BELOW the worst case — which force-kills a healthy shutdown.

This gate owns the arithmetic in ONE place and enforces three properties:

1. every force-kill limit (the compose ``stop_grace_period`` and each
   native ``WaitForExit`` budget) is STRICTLY greater than the computed
   natural worst case, so a healthy shutdown is never cut short;
2. every component of the total is read from its AUTHORITATIVE runtime
   source — the Dockerfile's uvicorn grace, the lifespan module's shutdown
   budget and processor-stop cap, the turn-finalizer drain's own constants,
   and the SQLite busy timeout that bounds the executor-join tail — so the
   gate cannot keep asserting a total the code no longer implements;
3. every surface that quotes the envelope quotes the SAME derivation
   VERBATIM (the canonical one-line form below), so a stale copy fails
   instead of silently contradicting its neighbours.

The teardown term is the one component with no runtime source: it is the
allowance for the interpreter's own cleanup after the last joinable
worker, so it is declared here as an explicit allowance.

Exit 0 with ``OK`` lines when all hold; exit 1 after printing the first
``FAIL:`` line otherwise. The repo root resolves relative to this script's
own location, so the gate runs from any working directory.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Authoritative runtime sources ──────────────────────────────────────────
# Each component is read from the code that enforces it; nothing here
# restates a number that the runtime does not own.

_DOCKERFILE = ROOT / "Dockerfile"
_LIFESPAN = ROOT / "backend" / "app" / "lifespan.py"
_CHAT = ROOT / "backend" / "app" / "api" / "routes" / "chat.py"
_DATABASE = ROOT / "backend" / "app" / "models" / "database.py"
_COMPOSE = ROOT / "docker-compose.yml"
_RELEASE = (
    ROOT
    / "docs"
    / "releases"
    / "pending"
    / "854-799-lifecycle-drain-shutdown-budget.md"
)

# The allowance for the interpreter's own cleanup after the last joinable
# worker has finished. No runtime constant owns this term; it is the only
# component declared here rather than read.
_TEARDOWN_ALLOWANCE_SECONDS = 5.0

_UVICORN_GRACE_RE = re.compile(
    r"--timeout-graceful-shutdown(?:\s+|=)(\d+)(?![\w])"
)
_LIFESPAN_BUDGET_RE = re.compile(
    r"^_SHUTDOWN_BUDGET_SECONDS\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE
)
_PROCESSOR_CAP_RE = re.compile(
    r"^_PROCESSOR_STOP_CAP_SECONDS\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE
)
_TURN_CONST_RE = re.compile(
    r"^(TURN_(?:PRODUCER_SHUTDOWN|FINALIZE_DRAIN)_TIMEOUT_SECONDS)\s*=\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*$",
    re.MULTILINE,
)
_BUSY_TIMEOUT_RE = re.compile(r"PRAGMA\s+busy_timeout\s*=\s*(\d+)")

# The compose grace and the native grouped-backend stop waits.
_COMPOSE_GRACE_RE = re.compile(r"^\s*stop_grace_period:\s*(\S+)\s*$", re.MULTILINE)
_WAITFOREXIT_RE = re.compile(r"WaitForExit\(\s*([0-9]+)\s*\)")

_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

# The surfaces that must carry the derivation verbatim.
_PS1_SURFACES = ("start-services.ps1", "stop-services.ps1")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _seconds(value: float) -> str:
    """Render a component's seconds the way the surfaces quote it."""
    return f"{value:g}"


def _parse_duration(value: str) -> float | None:
    """Compose duration (``90s``, ``1m30s``) to seconds; unitless = seconds.

    Returns None when the value contains no numeric component at all.
    """
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)([smhd]?)", value):
        total += float(amount) * _UNIT_SECONDS.get(unit or "s", 1.0)
        matched = True
    return total if matched else None


def _normalize(text: str) -> str:
    """Collapse whitespace so a wrapped derivation still matches."""
    return re.sub(r"\s+", " ", text)


def _components() -> tuple[list[tuple[str, float]], str]:
    """Read every component from its authoritative source.

    Returns the (label, seconds) list and the canonical one-line
    derivation. Raises ValueError with the offending source when a
    component cannot be read — a gate that cannot see a component must
    fail loudly, not fall back to a restated constant.
    """
    dockerfile = _read(_DOCKERFILE)
    grace = _UVICORN_GRACE_RE.search(dockerfile)
    if grace is None:
        raise ValueError(
            "Dockerfile has no --timeout-graceful-shutdown integer flag"
        )
    uvicorn_grace = float(grace.group(1))

    lifespan = _read(_LIFESPAN)
    budget = _LIFESPAN_BUDGET_RE.search(lifespan)
    if budget is None:
        raise ValueError("backend/app/lifespan.py has no _SHUTDOWN_BUDGET_SECONDS")
    cap = _PROCESSOR_CAP_RE.search(lifespan)
    if cap is None:
        raise ValueError(
            "backend/app/lifespan.py has no _PROCESSOR_STOP_CAP_SECONDS"
        )
    lifespan_aggregate = float(budget.group(1)) + float(cap.group(1))

    chat = _read(_CHAT)
    turn = dict(
        (name, float(value)) for name, value in _TURN_CONST_RE.findall(chat)
    )
    for required in (
        "TURN_PRODUCER_SHUTDOWN_TIMEOUT_SECONDS",
        "TURN_FINALIZE_DRAIN_TIMEOUT_SECONDS",
    ):
        if required not in turn:
            raise ValueError(f"backend/app/api/routes/chat.py has no {required}")
    turn_drain = (
        turn["TURN_PRODUCER_SHUTDOWN_TIMEOUT_SECONDS"]
        + turn["TURN_FINALIZE_DRAIN_TIMEOUT_SECONDS"]
    )

    database = _read(_DATABASE)
    busy = _BUSY_TIMEOUT_RE.search(database)
    if busy is None:
        raise ValueError(
            "backend/app/models/database.py has no PRAGMA busy_timeout"
        )
    join_tail = float(busy.group(1)) / 1000.0

    components = [
        ("uvicorn request drain", uvicorn_grace),
        ("lifespan aggregate", lifespan_aggregate),
        ("turn-finalizer drain", turn_drain),
        ("SQLite-family executor-join tail", join_tail),
        ("teardown", _TEARDOWN_ALLOWANCE_SECONDS),
    ]
    total = sum(seconds for _label, seconds in components)
    derivation = (
        " + ".join(
            f"{_seconds(seconds)} s ({label})" for label, seconds in components
        )
        + f" = {_seconds(total)} s"
    )
    return components, derivation


def main() -> int:
    try:
        components, derivation = _components()
    except ValueError as exc:
        print(f"FAIL: cannot read an envelope component — {exc}")
        return 1
    total = sum(seconds for _label, seconds in components)
    print(f"OK natural worst case = {derivation}")

    # ── 1. compose stop_grace_period ──────────────────────────────────────
    compose_text = _read(_COMPOSE)
    grace_match = _COMPOSE_GRACE_RE.search(compose_text)
    if grace_match is None:
        print("FAIL: docker-compose.yml has no stop_grace_period")
        return 1
    grace_value = grace_match.group(1)
    grace_seconds = _parse_duration(grace_value)
    if grace_seconds is None:
        print(
            "FAIL: cannot parse docker-compose.yml stop_grace_period value "
            f"{grace_value!r} as a duration"
        )
        return 1
    if not grace_seconds > total:
        print(
            f"FAIL: docker-compose stop_grace_period ({grace_value} = "
            f"{grace_seconds:g}s) must be strictly greater than the "
            f"{total:g}s natural worst case"
        )
        return 1
    print(
        f"OK docker-compose stop_grace_period {grace_value} "
        f"({grace_seconds:g}s) exceeds the {total:g}s worst case"
    )

    # ── 1+2. native WaitForExit budgets ───────────────────────────────────
    # Each .ps1 has TWO waits: the graceful window for the GROUPED backend
    # (the one this envelope bounds) and a short fallback wait for a
    # process with no marker match (a different path, not this envelope).
    # Only the grouped graceful wait is bound by the worst case, so it is
    # located by the CTRL_BREAK send that precedes it.
    for name in _PS1_SURFACES:
        ps1_text = _read(ROOT / name)
        lines = ps1_text.splitlines()
        send_index = next(
            (
                index
                for index, line in enumerate(lines)
                if "CTRL_BREAK" in line and "graceful" in line
            ),
            None,
        )
        if send_index is None:
            print(f"FAIL: {name} does not send a graceful CTRL_BREAK")
            return 1
        grouped_wait_ms = next(
            (
                int(match.group(1))
                for line in lines[send_index:]
                if (match := _WAITFOREXIT_RE.search(line))
            ),
            None,
        )
        if grouped_wait_ms is None:
            print(
                f"FAIL: {name} sends a graceful CTRL_BREAK but never waits "
                "for the process to exit"
            )
            return 1
        wait_seconds = grouped_wait_ms / 1000.0
        if not wait_seconds > total:
            print(
                f"FAIL: {name} grouped-backend WaitForExit({grouped_wait_ms}) "
                f"= {wait_seconds:g}s must be strictly greater than the "
                f"{total:g}s natural worst case"
            )
            return 1
        print(
            f"OK {name} grouped-backend native stop wait "
            f"{grouped_wait_ms}ms ({wait_seconds:g}s) exceeds the "
            f"{total:g}s worst case"
        )

    # ── 2. every surface carries the derivation VERBATIM ───────────────────
    # The check is on the whole canonical line, not on a bare number: a
    # stale copy that happens to contain the same digits elsewhere cannot
    # satisfy it.
    surfaces = [
        ("docker-compose.yml", compose_text),
        ("start-services.ps1", _read(ROOT / "start-services.ps1")),
        ("stop-services.ps1", _read(ROOT / "stop-services.ps1")),
        ("docs/releases/pending/854-799-lifecycle-drain-shutdown-budget.md", _read(_RELEASE)),
        ("backend/app/lifespan.py", _read(_LIFESPAN)),
    ]
    canonical = _normalize(derivation)
    for name, text in surfaces:
        if canonical not in _normalize(text):
            print(
                f"FAIL: {name} does not quote the shutdown envelope "
                f"derivation verbatim — expected: {derivation}"
            )
            return 1
        print(f"OK {name} quotes the derivation verbatim")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
