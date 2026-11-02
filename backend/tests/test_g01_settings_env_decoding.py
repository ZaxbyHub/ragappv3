"""Issue #734 acceptance checks — env/dotenv values for complex-typed
Settings fields must construct without a SettingsError.

pydantic-settings JSON-decodes complex-typed fields (``allowed_hosts``,
``allowed_extensions``, ``imap_allowed_mime_types``) BEFORE mode="before"
validators run, so any non-JSON env/dotenv value (empty string, plain
comma-separated list) makes ``Settings()`` raise. ``settings = Settings()``
runs at module scope, so ``import app.config`` in a fresh subprocess is the
faithful trigger.

RED at the base tree (SettingsError before the fix), GREEN after the fix.

Every check imports ``app.config`` in a fresh subprocess with an empty temp
cwd (never the real backend cwd, so a developer's live ``backend/.env`` can
mask or pollute the result) and only the two required secrets plus the
variable under test in the process environment (inherited from os.environ,
then overridden).
"""

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

_SUBPROCESS_TIMEOUT_S = 120


def _run_import_subprocess(code: str, extra_env: dict[str, str], cwd: Path):
    """Run ``python -c <code>`` in a fresh subprocess with inherited env.

    Inherits os.environ, then forces a valid backend on PYTHONPATH, the two
    required secrets, and the caller's per-test overrides.
    """
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(BACKEND_DIR),
            "ADMIN_SECRET_TOKEN": "a" * 48,
            "JWT_SECRET_KEY": "b" * 48,
        }
    )
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def _assert_import_succeeded(result, env_name: str, env_value: str) -> None:
    """Assert returncode == 0 FIRST, with the stderr tail in the message."""
    assert result.returncode == 0, (
        f"import app.config exited {result.returncode} with "
        f"{env_name}={env_value!r}\n"
        f"stderr tail:\n{result.stderr[-2000:]}"
    )


def _last_stdout_value(result):
    """Literal-eval the last stdout line (the printed repr of a setting)."""
    return ast.literal_eval(result.stdout.strip().splitlines()[-1])


def test_allowed_hosts_empty_env_constructs():
    """ALLOWED_HOSTS="" must construct (empty -> []) instead of SettingsError."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_import_subprocess(
            "import app.config; print(repr(app.config.settings.allowed_hosts))",
            {"ALLOWED_HOSTS": ""},
            cwd=Path(tmp),
        )
        _assert_import_succeeded(result, "ALLOWED_HOSTS", "")
        assert _last_stdout_value(result) == [], (
            f"allowed_hosts should be [] for empty env, got:\n{result.stdout[-500:]}"
        )


def test_allowed_hosts_comma_env_parses():
    """ALLOWED_HOSTS as a plain comma-separated value must parse to a list."""
    value = "a.example.com, b.example.com"
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_import_subprocess(
            "import app.config; print(repr(app.config.settings.allowed_hosts))",
            {"ALLOWED_HOSTS": value},
            cwd=Path(tmp),
        )
        _assert_import_succeeded(result, "ALLOWED_HOSTS", value)
        assert _last_stdout_value(result) == ["a.example.com", "b.example.com"], (
            f"allowed_hosts should parse as comma-separated, got:\n{result.stdout[-500:]}"
        )


def test_allowed_extensions_comma_env_parses():
    """ALLOWED_EXTENSIONS as a plain comma-separated value must parse to a set."""
    value = ".txt,.md,.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_import_subprocess(
            "import app.config; print(repr(app.config.settings.allowed_extensions))",
            {"ALLOWED_EXTENSIONS": value},
            cwd=Path(tmp),
        )
        _assert_import_succeeded(result, "ALLOWED_EXTENSIONS", value)
        assert _last_stdout_value(result) == {".txt", ".md", ".pdf"}, (
            f"allowed_extensions should parse as comma-separated, got:\n{result.stdout[-500:]}"
        )


def test_allowed_extensions_empty_env_constructs():
    """ALLOWED_EXTENSIONS="" must construct instead of raising SettingsError.

    Returncode-only: whether empty maps to the code default or an empty set
    belongs to the fix, not to this check.
    """
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_import_subprocess(
            "import app.config",
            {"ALLOWED_EXTENSIONS": ""},
            cwd=Path(tmp),
        )
        _assert_import_succeeded(result, "ALLOWED_EXTENSIONS", "")


def test_imap_mime_types_comma_env_parses():
    """IMAP_ALLOWED_MIME_TYPES as a comma-separated value must parse to a set."""
    value = "text/plain, text/html, application/pdf, image/png"
    expected = {"text/plain", "text/html", "application/pdf", "image/png"}
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_import_subprocess(
            "import app.config; print(repr(app.config.settings.imap_allowed_mime_types))",
            {"IMAP_ALLOWED_MIME_TYPES": value},
            cwd=Path(tmp),
        )
        _assert_import_succeeded(result, "IMAP_ALLOWED_MIME_TYPES", value)
        assert _last_stdout_value(result) == expected, (
            f"imap_allowed_mime_types should parse as comma-separated, got:\n{result.stdout[-500:]}"
        )


def test_env_example_verbatim_dotenv_constructs():
    """A verbatim copy of the tracked repo-root .env.example as .env must
    construct when only the two required secrets come from the process env.

    RED at the base tree (DotEnvSettingsSource raises SettingsError on the
    non-JSON complex-typed values), GREEN after the fix.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        env_example = REPO_ROOT / ".env.example"
        (tmp_path / ".env").write_text(
            env_example.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = _run_import_subprocess("import app.config", {}, cwd=tmp_path)
        _assert_import_succeeded(
            result, ".env", f"verbatim copy of {env_example}"
        )
