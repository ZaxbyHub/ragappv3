#!/usr/bin/env python3
"""Check the process-shutdown envelope's ONE derivation (#799 AC5 / #854).

The composed shutdown envelope is a SUM of windows. Written by hand in
several places (docker-compose.yml, both .ps1 launchers, the lifespan
module, the release fragment) the copies drift and the force-kill
boundary can end up BELOW the envelope — which force-kills a healthy
shutdown.

The derivation has two halves, and this gate keeps them apart:

* ENFORCED terms — each one is read from the code that enforces it, so
  the gate cannot assert a number the runtime does not implement:
  the Dockerfile's operative uvicorn grace, the lifespan module's
  shutdown budget and processor-stop cap, and the turn-finalizer
  drain's own constants;
* ALLOWANCE terms — the executor-join tail and the interpreter
  teardown are NOT bounded by any code (the abandoned-parse worker is
  the documented case), so they are declared here as explicit policy
  allowances, not as guarantees.

The gate enforces three properties:

1. every force-kill limit (the compose ``stop_grace_period`` and each
   native ``WaitForExit`` budget) is STRICTLY greater than the composed
   total, so a shutdown that stays inside the enforced terms is never
   cut short;
2. every enforced term is read from its authoritative runtime source —
   a component that drifts in the code changes the derived total and
   fails every surface that still quotes the old one;
3. every surface that quotes the envelope quotes the SAME derivation
   VERBATIM (the canonical one-line form below), so a stale copy fails
   instead of silently contradicting its neighbours.

Exit 0 with ``OK`` lines when all hold; exit 1 after printing the first
``FAIL:`` line otherwise. The repo root resolves relative to this script's
own location, so the gate runs from any working directory.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Authoritative runtime sources ──────────────────────────────────────────
_DOCKERFILE = ROOT / "Dockerfile"
_LIFESPAN = ROOT / "backend" / "app" / "lifespan.py"
_CHAT = ROOT / "backend" / "app" / "api" / "routes" / "chat.py"
_COMPOSE = ROOT / "docker-compose.yml"
_RELEASE = (
    ROOT
    / "docs"
    / "releases"
    / "pending"
    / "854-799-lifecycle-drain-shutdown-budget.md"
)

# ── Allowance terms (no runtime source owns these) ─────────────────────────
# The SQLite-family executor join tail: the executor threads are joined at
# interpreter exit, and a worker whose await was abandoned keeps running
# (document_processor.py's abandoned-parse contract) — NO code bounds that
# join, so this term is an ALLOWANCE, not a bound. In the shipped
# configurations the bounded tail exit (State A) skips those joins
# entirely; this allowance is the slack the force limits leave for the
# path where the tail exit declines.
_JOIN_TAIL_ALLOWANCE_SECONDS = 30.0
# The interpreter's own teardown after the last joinable worker finished.
# Also an ALLOWANCE.
_TEARDOWN_ALLOWANCE_SECONDS = 5.0

_UVICORN_GRACE_RE = re.compile(
    r"--timeout-graceful-shutdown(?:\s+|=)(\d+)(?![\w])"
)
_CMD_RE = re.compile(r"^\s*CMD\s", re.MULTILINE)
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


def _uvicorn_grace_seconds() -> float:
    """The uvicorn grace from the Dockerfile's OPERATIVE CMD line.

    Two things make the naive search wrong:

    * the Dockerfile also mentions the flag in a COMMENT above the CMD,
      and the comment is not what uvicorn reads;
    * Docker uses the LAST CMD instruction (of the final stage), so an
      earlier CMD carrying the flag does not decide the runtime value.

    So: take the LAST CMD line in the file and read the flag from THAT
    line. If the operative CMD carries no flag, the runtime has no bound
    and the gate fails closed.
    """
    dockerfile = _read(_DOCKERFILE)
    cmd_lines = [
        line for line in dockerfile.splitlines() if _CMD_RE.match(line)
    ]
    if not cmd_lines:
        raise ValueError("Dockerfile has no CMD line")
    operative = cmd_lines[-1]
    flags = _UVICORN_GRACE_RE.findall(operative)
    if not flags:
        raise ValueError(
            "Dockerfile's operative CMD carries no --timeout-graceful-shutdown "
            "integer flag"
        )
    # argparse resolves a repeated option by letting the LAST occurrence
    # win, so that is the value uvicorn actually receives; the gate must
    # read the same one (the first occurrence would be a value the runtime
    # never uses).
    return float(flags[-1])


def _enforced_components() -> list[tuple[str, float]]:
    """Read every ENFORCED component from its authoritative source."""
    uvicorn_grace = _uvicorn_grace_seconds()

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

    return [
        ("uvicorn request drain", uvicorn_grace),
        ("lifespan aggregate", lifespan_aggregate),
        ("turn-finalizer drain", turn_drain),
    ]


def _allowance_components() -> list[tuple[str, float]]:
    """The terms no runtime source owns (declared as policy allowances)."""
    return [
        ("SQLite-family executor-join tail", _JOIN_TAIL_ALLOWANCE_SECONDS),
        ("teardown", _TEARDOWN_ALLOWANCE_SECONDS),
    ]


def _derivation() -> tuple[str, float]:
    """The canonical one-line derivation and the composed total."""
    enforced = _enforced_components()
    allowance = _allowance_components()
    enforced_total = sum(seconds for _label, seconds in enforced)
    allowance_total = sum(seconds for _label, seconds in allowance)
    total = enforced_total + allowance_total

    def _parts(components: list[tuple[str, float]]) -> str:
        return " + ".join(
            f"{_seconds(seconds)} s ({label})" for label, seconds in components
        )

    canonical = (
        f"enforced: {_parts(enforced)} = {_seconds(enforced_total)} s; "
        f"allowance: {_parts(allowance)} = {_seconds(allowance_total)} s; "
        f"total {_seconds(total)} s"
    )
    return canonical, total


def main() -> int:
    try:
        canonical, total = _derivation()
    except ValueError as exc:
        print(f"FAIL: cannot read an envelope component — {exc}")
        return 1
    print(f"OK composed shutdown envelope = {canonical}")

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
            f"{total:g}s composed envelope"
        )
        return 1
    print(
        f"OK docker-compose stop_grace_period {grace_value} "
        f"({grace_seconds:g}s) exceeds the {total:g}s envelope"
    )

    # ── 1. native WaitForExit budgets ─────────────────────────────────────
    # Each .ps1 has TWO waits: the graceful window for the GROUPED backend
    # (the one this envelope bounds) and a short fallback wait for a
    # process with no marker match (a different path, not this envelope).
    # Only the grouped graceful wait is bound by the envelope, so it is
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
                f"{total:g}s composed envelope"
            )
            return 1
        print(
            f"OK {name} grouped-backend native stop wait "
            f"{grouped_wait_ms}ms ({wait_seconds:g}s) exceeds the "
            f"{total:g}s envelope"
        )

    # ── 2. every surface carries the derivation VERBATIM ───────────────────
    # The check is on the whole canonical line, not on a bare number: a
    # stale copy that happens to contain the same digits elsewhere cannot
    # satisfy it.
    surfaces = [
        ("docker-compose.yml", compose_text),
        ("start-services.ps1", _read(ROOT / "start-services.ps1")),
        ("stop-services.ps1", _read(ROOT / "stop-services.ps1")),
        (
            "docs/releases/pending/854-799-lifecycle-drain-shutdown-budget.md",
            _read(_RELEASE),
        ),
        ("backend/app/lifespan.py", _read(_LIFESPAN)),
    ]
    normalized = _normalize(canonical)
    for name, text in surfaces:
        if normalized not in _normalize(text):
            print(
                f"FAIL: {name} does not quote the shutdown envelope "
                f"derivation verbatim — expected: {canonical}"
            )
            return 1
        print(f"OK {name} quotes the derivation verbatim")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
