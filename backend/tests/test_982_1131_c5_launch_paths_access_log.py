"""Acceptance check C5 (issue #982 Family C): shipped non-container launch
paths must disable uvicorn's raw access log.

uvicorn's access log re-emits the raw request line (including the query
string: ``GET /api/v1/search?q=<user text>``) at INFO through the
``uvicorn.access`` logger, which has ``propagate=False`` and its own
handlers — the app's SensitiveFieldFilter (root handlers only) never sees
those records. The container launch already pins ``--no-access-log``
(Dockerfile:139, pinned by test_c10_access_log_query_redaction.py); the
four shipped NON-container launch paths and the CI schemathesis server do
not.

This check pins the flag on those five surfaces (static content assertions
following the Dockerfile pin's shape). RED at base, GREEN after the fix.
docs/admin-guide.md is deliberately NOT pinned here (design may change
wording — that surface belongs to docs review, not a frozen check).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# A "launch" line actually starts a uvicorn server for the app (the prose
# mentions in INSTALLATION.md — ":319", ":807", ":848" — never carry the
# trailing ` --` flags, so this discriminates commands from prose).
_UVICORN_LAUNCH = re.compile(r"(python\s+-m\s+uvicorn|uvicorn)\s+app\.main:app\s+--")


def _launch_lines(text: str, filename: str):
    lines = [line for line in text.splitlines() if _UVICORN_LAUNCH.search(line)]
    assert lines, f"982C5: no uvicorn launch line found in {filename}"
    return lines


def test_start_services_ps1_disables_access_log():
    """start-services.ps1's backend launch must pass --no-access-log."""
    script = REPO_ROOT / "start-services.ps1"
    assert script.exists(), f"982C5: start-services.ps1 expected at {script}"
    text = script.read_text(encoding="utf-8", errors="replace")
    arglists = [
        line for line in text.splitlines() if "uvicorn" in line and "app.main:app" in line
    ]
    assert arglists, "982C5: the backend uvicorn ArgumentList line must exist"
    joined = "\n".join(arglists)
    assert "--no-access-log" in joined, (
        "982C5: start-services.ps1 launches uvicorn without --no-access-log; "
        "the raw access log (query strings included) bypasses the app's "
        f"redaction filters on this shipped native path: {arglists}"
    )


def test_installation_md_launches_disable_access_log():
    """Every uvicorn launch command in INSTALLATION.md must carry the flag."""
    doc = REPO_ROOT / "INSTALLATION.md"
    assert doc.exists(), f"982C5: INSTALLATION.md expected at {doc}"
    lines = _launch_lines(doc.read_text(encoding="utf-8", errors="replace"),
                          "INSTALLATION.md")
    missing = [line for line in lines if "--no-access-log" not in line]
    assert missing == [], (
        "982C5: INSTALLATION.md uvicorn launch command(s) lack "
        f"--no-access-log (raw access log on a shipped native path): "
        f"{missing}"
    )


def test_contributing_md_launch_disables_access_log():
    """CONTRIBUTING.md's dev-server launch must carry the flag."""
    doc = REPO_ROOT / "CONTRIBUTING.md"
    assert doc.exists(), f"982C5: CONTRIBUTING.md expected at {doc}"
    lines = _launch_lines(doc.read_text(encoding="utf-8", errors="replace"),
                          "CONTRIBUTING.md")
    missing = [line for line in lines if "--no-access-log" not in line]
    assert missing == [], (
        "982C5: CONTRIBUTING.md uvicorn launch command(s) lack "
        f"--no-access-log (raw access log on a shipped native path): "
        f"{missing}"
    )


def test_ci_schemathesis_server_disables_access_log():
    """The nightly schemathesis server (whose uvicorn log is uploaded as a
    workflow artifact) must carry the flag."""
    workflow = REPO_ROOT / ".github" / "workflows" / "nightly-quality-gates.yml"
    assert workflow.exists(), "982C5: nightly-quality-gates.yml expected"
    text = workflow.read_text(encoding="utf-8", errors="replace")
    # The launch is a shell continuation: join the command line with its
    # backslash-continued followers before checking the flag.
    out_lines = text.splitlines()
    commands = []
    for index, line in enumerate(out_lines):
        if "python -m uvicorn" in line or re.search(r"\buvicorn\s+app\.main:app", line):
            command = line
            cursor = index
            while command.rstrip().endswith("\\") and cursor + 1 < len(out_lines):
                cursor += 1
                command += "\n" + out_lines[cursor]
            commands.append(command)
    assert commands, "982C5: the schemathesis uvicorn launch must exist"
    missing = [c for c in commands if "--no-access-log" not in c]
    assert missing == [], (
        "982C5: the CI schemathesis uvicorn server lacks --no-access-log; "
        "its log is uploaded as a workflow artifact with raw query strings: "
        f"{missing}"
    )
