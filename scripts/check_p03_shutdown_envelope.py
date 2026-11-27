#!/usr/bin/env python3
"""Check the process-shutdown envelope's ONE derivation (#799 AC5 / #854).

The natural worst case of a graceful shutdown is a SUM of independently
enforced windows. When that total is written by hand in several places
(docker-compose.yml, both .ps1 launchers, the lifespan module comment, the
release fragment), the copies drift and the force-kill boundary can end up
BELOW the worst case — which force-kills a healthy shutdown.

This gate owns the arithmetic in ONE place and enforces three properties:

1. every force-kill limit (the compose ``stop_grace_period`` and each native
   ``WaitForExit`` budget) is STRICTLY greater than the computed natural worst
   case, so a healthy shutdown is never cut short;
2. every surface that quotes the envelope quotes the SAME worst-case total
   (the component seconds are re-derived here, and the surfaces must carry
   the resulting number), so a stale copy fails instead of silently
   contradicting its neighbours;
3. the derived worst case is itself the sum of the components this script
   lists — the components live here and nowhere else.

The ``_SHUTDOWN_BUDGET_SECONDS`` lifespan aggregate is deliberately NOT
duplicated here: it is a source-level constant, and this gate reads it from
the lifespan module so the two cannot disagree.

Exit 0 with ``OK`` lines when all hold; exit 1 after printing the first
``FAIL:`` line otherwise. The repo root resolves relative to this script's
own location, so the gate runs from any working directory.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── The components of the natural worst case ───────────────────────────────
# Each entry: (label, seconds). The sum is the natural worst case.
#
#   uvicorn request drain  — Dockerfile --timeout-graceful-shutdown 20
#   lifespan aggregate      — the 60 s budget PLUS the 5 s outer cap the
#                             lifespan puts on the processor stop
#   turn-finalizer drain   — 5 s producer retirement + 10 s drain
#   SQLite-family join tail— the executor joins that outlive the lifespan
#   teardown               — interpreter-level cleanup
_COMPONENTS: tuple[tuple[str, float], ...] = (
    ("uvicorn request drain", 20.0),
    ("lifespan aggregate", 65.0),
    ("turn-finalizer drain", 15.0),
    ("SQLite-family executor-join tail", 30.0),
    ("teardown", 5.0),
)

# The lifespan module's aggregate budget, read from source so this script and
# the implementation cannot drift apart.
_LIFESPAN_BUDGET_RE = re.compile(
    r"^_SHUTDOWN_BUDGET_SECONDS\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE
)

# The 5 s outer cap the lifespan puts on the processor stop (on top of the
# aggregate budget).
_PROCESSOR_STOP_CAP_SECONDS = 5.0

_COMPOSE_GRACE_RE = re.compile(r"^\s*stop_grace_period:\s*(\S+)\s*$", re.MULTILINE)
_WAITFOREXIT_RE = re.compile(r"WaitForExit\(\s*([0-9]+)\s*\)")

_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

_PS1_SURFACES = ("start-services.ps1", "stop-services.ps1")


def _quotes(text: str, seconds: float) -> bool:
    """True when ``text`` quotes ``seconds`` (with or without a space).

    The surfaces render the total as ``135 s`` in prose and ``135`` inside a
    duration literal, so the whitespace between the number and the unit is
    not significant.
    """
    return f"{seconds:g}".replace(".", "") in re.sub(r"\s+", "", text)


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


def _lifespan_budget_seconds() -> float | None:
    lifespan_text = (ROOT / "backend" / "app" / "lifespan.py").read_text(
        encoding="utf-8"
    )
    match = _LIFESPAN_BUDGET_RE.search(lifespan_text)
    return float(match.group(1)) if match else None


def main() -> int:
    lifespan_budget = _lifespan_budget_seconds()
    if lifespan_budget is None:
        print("FAIL: backend/app/lifespan.py has no _SHUTDOWN_BUDGET_SECONDS")
        return 1
    expected_lifespan_aggregate = lifespan_budget + _PROCESSOR_STOP_CAP_SECONDS
    declared = dict(_COMPONENTS)
    if declared["lifespan aggregate"] != expected_lifespan_aggregate:
        print(
            "FAIL: the lifespan aggregate component "
            f"({declared['lifespan aggregate']:g}s) must be the "
            f"_SHUTDOWN_BUDGET_SECONDS ({lifespan_budget:g}s) plus the "
            f"{_PROCESSOR_STOP_CAP_SECONDS:g}s processor-stop cap"
        )
        return 1

    worst_case = sum(seconds for _label, seconds in _COMPONENTS)
    print(
        "OK natural worst case = "
        + " + ".join(f"{seconds:g}" for _label, seconds in _COMPONENTS)
        + f" = {worst_case:g}s"
    )

    # ── 1+2. compose stop_grace_period ─────────────────────────────────────
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
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
    if not grace_seconds > worst_case:
        print(
            f"FAIL: docker-compose stop_grace_period ({grace_value} = "
            f"{grace_seconds:g}s) must be strictly greater than the "
            f"{worst_case:g}s natural worst case"
        )
        return 1
    if not _quotes(compose_text, worst_case):
        print(
            f"FAIL: docker-compose.yml does not quote the {worst_case:g}s "
            "natural worst case — the envelope derivation drifted from the "
            "one place it is computed"
        )
        return 1
    print(
        f"OK docker-compose stop_grace_period {grace_value} "
        f"({grace_seconds:g}s) exceeds the {worst_case:g}s worst case"
    )

    # ── 1+2. native WaitForExit budgets ────────────────────────────────────
    # Each .ps1 has TWO waits: the graceful window for the GROUPED backend
    # (the one this envelope bounds) and a short fallback wait for a process
    # with no marker match (a different path, not this envelope). Only the
    # grouped graceful wait is bound by the worst case, so it is located by
    # the CTRL_BREAK send that precedes it.
    for name in _PS1_SURFACES:
        ps1_text = (ROOT / name).read_text(encoding="utf-8")
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
        if not wait_seconds > worst_case:
            print(
                f"FAIL: {name} grouped-backend WaitForExit({grouped_wait_ms}) "
                f"= {wait_seconds:g}s must be strictly greater than the "
                f"{worst_case:g}s natural worst case"
            )
            return 1
        if not _quotes(ps1_text, worst_case):
            print(
                f"FAIL: {name} does not quote the {worst_case:g}s natural "
                "worst case — the envelope derivation drifted from the one "
                "place it is computed"
            )
            return 1
        print(
            f"OK {name} grouped-backend native stop wait "
            f"{grouped_wait_ms}ms ({wait_seconds:g}s) exceeds the "
            f"{worst_case:g}s worst case"
        )

    # ── 2. the lifespan module's own comment quotes the same total ─────────
    lifespan_text = (ROOT / "backend" / "app" / "lifespan.py").read_text(
        encoding="utf-8"
    )
    if not _quotes(lifespan_text, worst_case):
        print(
            f"FAIL: backend/app/lifespan.py does not quote the "
            f"{worst_case:g}s natural worst case — the envelope derivation "
            "drifted from the one place it is computed"
        )
        return 1
    print(
        f"OK backend/app/lifespan.py quotes the {worst_case:g}s natural "
        "worst case"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
