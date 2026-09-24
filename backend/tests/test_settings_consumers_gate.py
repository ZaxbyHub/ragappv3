"""CI gate wrapper for scripts/check_settings_consumers.py (issue #662).

Mirrors the test_issue258_installation_doc.py wrapper pattern: spawning the
contract script is itself a CI gate, because the Backend job runs the full
pytest suite. Covers the census contract end to end:

- T1 the full census over the real tree exits 0,
- T2 a synthetic dormant field in a fixture tree exits 1 and is named,
- T3 a multi-line ``getattr`` consumer counts as a read (the shape a regex
  census missed in the issue's own self-correction),
- T4 the script's AST field enumeration matches ``Settings.model_fields``,
- T5 the allowlist contract (reasoned entries pass; empty reason or unknown
  field fail),
- T6 textual presence is not consumption (comment / string literal /
  Store-context write do not mint a consumer),
- T7 the census is wired into the ci.yml quality-contracts job and the
  justfile quality-contracts recipe.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "check_settings_consumers.py"
sys.path.insert(0, str(REPO / "scripts"))

import check_settings_consumers as gate  # noqa: E402

CONFIG_TPL = """from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    consumed_fixture_field: int = 1
{extra}


settings = Settings()
"""

CONSUMER_TPL = """from app.config import settings


def read_consumed() -> int:
    return settings.consumed_fixture_field
"""


def _make_fixture(root: Path, extra_fields: str) -> Path:
    (root / "backend" / "app").mkdir(parents=True, exist_ok=True)
    (root / "backend" / "app" / "services").mkdir(parents=True, exist_ok=True)
    (root / "backend" / "app" / "config.py").write_text(
        CONFIG_TPL.format(extra=extra_fields), encoding="utf-8"
    )
    (root / "backend" / "app" / "services" / "consumer.py").write_text(
        CONSUMER_TPL, encoding="utf-8"
    )
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_t1_full_census_green_on_real_tree():
    """T1: the census over the real post-disposition tree exits 0."""
    result = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "scripts/check_settings_consumers.py must pass on the real tree:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_t2_synthetic_dormant_field_fails_census_named(tmp_path):
    """T2 (AC4 demo): a dormant fixture field forces exit 1 and is named."""
    fixture = _make_fixture(tmp_path, "    synthetic_dormant_field: int = 2")
    result = _run(fixture)
    assert result.returncode == 1, f"expected exit 1, got {result.returncode}"
    assert "synthetic_dormant_field" in result.stdout, result.stdout


def test_t3_multiline_getattr_counts_as_consumer(tmp_path):
    """T3 (AC3 demo): multi-line getattr is a read; census on fixture is green."""
    fixture = _make_fixture(tmp_path, '    consumed_getattr_field: str = "m"')
    consumer = fixture / "backend" / "app" / "services" / "consumer.py"
    consumer.write_text(
        'from app.config import settings\n\n\n'
        "def read_attr() -> int:\n"
        "    return settings.consumed_fixture_field\n"
        "\n"
        "def read_multiline() -> str:\n"
        "    return getattr(\n"
        "        settings,\n"
        '        "consumed_getattr_field",\n'
        '        "fallback",\n'
        "    )\n",
        encoding="utf-8",
    )
    result = _run(fixture)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_t4_ast_enumeration_matches_model_fields():
    """T4: the script's AST enumeration equals pydantic's model_fields."""
    from app.config import Settings

    enumerated = gate.enumerate_settings_fields(REPO / "backend" / "app" / "config.py")
    assert set(enumerated) == set(Settings.model_fields), (
        "AST enumeration drifted from Settings.model_fields — update "
        "enumerate_settings_fields to stay pydantic-faithful"
    )


def test_t5_allowlist_contract(tmp_path):
    """T5: reasoned allowlist entries pass; empty reason and unknown field fail."""
    fixture = _make_fixture(tmp_path, "    dormant_with_reason: int = 3")
    allowlist = tmp_path / "allowlist.txt"

    allowlist.write_text(
        "dormant_with_reason :: pending wiring of the frobnicator :: backend owner\n",
        encoding="utf-8",
    )
    result = _run(fixture, "--allowlist", str(allowlist))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    allowlist.write_text("dormant_with_reason ::\n", encoding="utf-8")
    result = _run(fixture, "--allowlist", str(allowlist))
    assert result.returncode == 1, "empty-reason allowlist entry must fail"

    allowlist.write_text("not_a_settings_field :: some reason\n", encoding="utf-8")
    result = _run(fixture, "--allowlist", str(allowlist))
    assert result.returncode == 1, "unknown-field allowlist entry must fail"


def test_t6_textual_presence_is_not_consumption(tmp_path):
    """T6: comment, string literal, and Store-context write do not mint a consumer."""
    fixture = _make_fixture(tmp_path, "    name_only_field: int = 4")
    consumer = fixture / "backend" / "app" / "services" / "consumer.py"
    consumer.write_text(
        'from app.config import settings\n\n\n'
        "def write_only() -> None:\n"
        "    # name_only_field appears here only in a comment\n"
        '    marker = "name_only_field"\n'
        "    settings.name_only_field = 1\n"
        "    return None\n",
        encoding="utf-8",
    )
    result = _run(fixture)
    assert result.returncode == 1, "textual presence must not count as consumption"
    assert "name_only_field" in result.stdout, result.stdout


def test_t7_census_wired_into_ci_and_justfile():
    """T7: the census runs inside the quality-contracts CI job and justfile recipe."""
    command = "python scripts/check_settings_consumers.py"

    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_lines = ci.splitlines()
    in_quality_job = False
    found_ci_step = False
    for line in ci_lines:
        if line.startswith("  quality-contracts:"):
            in_quality_job = True
            continue
        if in_quality_job and line.startswith("  ") and not line.startswith("    "):
            break  # next top-level job key ends the block
        if in_quality_job and not line.lstrip().startswith("#") and command in line:
            found_ci_step = True
            break
    assert found_ci_step, "ci.yml quality-contracts job must run the census"

    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    jf_lines = justfile.splitlines()
    in_recipe = False
    found_just_line = False
    for line in jf_lines:
        if line.startswith("quality-contracts:"):
            in_recipe = True
            continue
        if in_recipe and line and not line.startswith(" "):
            break  # next recipe ends the block
        if in_recipe and not line.lstrip().startswith("#") and command in line:
            found_just_line = True
            break
    assert found_just_line, "justfile quality-contracts recipe must run the census"
