"""Issue #738 wiring pin (G06): the setup-docs truth gate runs in CI.

Pins every surface that must name ``scripts/check_g06_setup_docs.py``: the
ci.yml quality-contracts step, the justfile recipe, the two enforced
engineering-doc inventories (``check_runtime_contract.py`` derives its
inventory from ci.yml and requires both docs name every script), the
AGENTS.md/CONTRIBUTING.md human-facing lists, and the ci-compatibility-audit
skill mirror. Also proves the inventory regex governs the digit-bearing name
("g06", same class as the b03 pin) and exercises the gate's own
missing-documentation and Annotated-declaration branches against mutated
copies (plan-critic round 1 findings 1 and 5).

Model: ``test_b03_contract_wiring.py`` (issue #692).
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMMAND = "python scripts/check_g06_setup_docs.py"
CHECKER = REPO / "scripts" / "check_runtime_contract.py"
GATE = REPO / "scripts" / "check_g06_setup_docs.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("check_g06_setup_docs", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_g06_guard_wired_into_ci_and_justfile():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_lines = ci.splitlines()
    in_quality_job = False
    found_ci_step = False
    for line in ci_lines:
        if line.startswith("  quality-contracts:"):
            in_quality_job = True
            continue
        if in_quality_job and line.strip() and not line.startswith("    "):
            break  # a 2-space-indented sibling job key ends the block
        if in_quality_job and not line.lstrip().startswith("#") and COMMAND in line:
            found_ci_step = True
            break
    assert found_ci_step, "ci.yml quality-contracts job must run the g06 guard"

    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    jf_lines = justfile.splitlines()
    in_recipe = False
    found_just_line = False
    for line in jf_lines:
        if line.startswith("quality-contracts:"):
            in_recipe = True
            continue
        if in_recipe and line and not line.startswith(" "):
            break
        if in_recipe and not line.lstrip().startswith("#") and COMMAND in line:
            found_just_line = True
            break
    assert found_just_line, "justfile quality-contracts recipe must run the g06 guard"


def test_g06_guard_governed_by_runtime_contract_inventory():
    spec = importlib.util.spec_from_file_location("check_runtime_contract", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.DOC_SCRIPT_RE.search(
        "scripts/check_g06_setup_docs.py"
    ), "the inventory regex must govern the digit-bearing script name"

    for doc in (
        "docs/engineering/testing.md",
        "docs/engineering/conventions.md",
    ):
        text = (REPO / doc).read_text(encoding="utf-8")
        assert "scripts/check_g06_setup_docs.py" in text, (
            f"{doc} must name the g06 guard in its contract-script inventory"
        )


def test_g06_guard_named_in_human_facing_lists():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "check_g06_setup_docs.py" in agents, (
        "AGENTS.md CI-gates list must name the g06 guard"
    )
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "check_g06_setup_docs.py" in contributing, (
        "CONTRIBUTING.md local-gates block must name the g06 guard"
    )
    skill = (
        REPO / ".agents" / "skills" / "ci-compatibility-audit" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert skill.count("check_g06_setup_docs.py") >= 2, (
        "the ci-compatibility-audit skill mirror must list the g06 guard in both "
        "the step list and the local-mirror block"
    )


def test_g06_redis_url_requires_documented_key():
    """A template with NO REDIS_URL entry (active or commented) fails the mode.

    The mode consults documented_env_entry first; these helper semantics are
    what turn a deleted key into the distinct missing-documentation FAIL
    (plan-critic round 1 finding 5).
    """
    gate = _load_gate_module()
    env_text = "# some unrelated template\nOTHER_KEY=1\n"
    assert gate.documented_env_entry(env_text, "REDIS_URL") is None
    assert gate.documented_env_entry("#REDIS_URL=redis://x\n", "REDIS_URL") == (
        1,
        "redis://x",
    )
    assert gate.active_env_entry("#REDIS_URL=redis://x\n", "REDIS_URL") is None
    assert gate.active_env_entry("REDIS_URL=redis://x\n", "REDIS_URL") == (
        1,
        "redis://x",
    )


def test_g06_data_dir_requires_documented_key():
    """A template with NO DATA_DIR entry (active or commented) fails the mode."""
    gate = _load_gate_module()
    assert gate.documented_env_entry("UNRELATED=1\n", "DATA_DIR") is None
    assert gate.documented_env_entry("#DATA_DIR=/app/data\n", "DATA_DIR") == (
        1,
        "/app/data",
    )


def _synthetic_repo_texts(
    env_text: str,
    *,
    config_text: str = (
        'document_parsing_strategy: str = "auto"\n'
        "allowed_extensions: Annotated[set[str], NoDecode] = {\n"
        '".txt",\n".md",\n".pdf",\n".pptx",\n'
        "} | set(RASTER_IMAGE_EXTENSIONS)\n"
    ),
    compose_text: str = "  - DATA_DIR=/app/data\n",
    contributing_text: str = (
        "**Environment:** copy `.env.example` to `backend/.env` (the backend "
        "reads `.env` from its own working directory) and fill in secrets."
    ),
):
    """Repo-file map driving the gate's `read` for mode-level tests."""
    return {
        ".env.example": env_text,
        "backend/app/config.py": config_text,
        "backend/app/services/document_artifacts.py": (
            'RASTER_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg"})'
        ),
        "docker-compose.yml": compose_text,
        "CONTRIBUTING.md": contributing_text,
    }


def test_g06_modes_fail_on_missing_documented_keys(monkeypatch):
    """The mode-level missing-documentation FAIL branches actually fire.

    Executes check_redis_url / check_data_dir / check_parsing_strategy /
    check_contributing_env_path against synthetic repo texts so a deleted
    guard cannot pass with the suite green (PRR-015).
    """
    gate = _load_gate_module()
    texts = _synthetic_repo_texts("OTHER=1\n")
    monkeypatch.setattr(gate, "read", lambda p: texts[p])

    redis_fail = gate.check_redis_url()
    assert redis_fail and "no REDIS_URL entry" in redis_fail[0]
    data_fail = gate.check_data_dir()
    assert data_fail and "no DATA_DIR entry" in data_fail[0]
    parsing_fail = gate.check_parsing_strategy()
    assert parsing_fail and "no DOCUMENT_PARSING_STRATEGY entry" in parsing_fail[0]

    # The contributing mode passes here (paragraph names backend/.env), then
    # fails when the paragraph is missing entirely (PRR-004 fail-open fix).
    assert gate.check_contributing_env_path() == []
    texts["CONTRIBUTING.md"] = "Some other section without the env step.\n"
    missing = gate.check_contributing_env_path()
    assert missing and "no **Environment:** paragraph" in missing[0]


def test_g06_contributing_env_path_requires_backend_env_name(monkeypatch):
    """An **Environment:** paragraph naming neither env file fails the mode.

    Pins the #1062 fail-open fix: a paragraph that exists but names neither
    `.env.example` nor `backend/.env` used to return [] (the FAIL required
    `.env.example` to be named), so the setup doc could silently lose the
    backend/.env pointer that #738 AC15 guarantees. The names-both pass and
    missing-paragraph FAIL branches are pinned separately by
    test_g06_modes_fail_on_missing_documented_keys.
    """
    gate = _load_gate_module()
    texts = _synthetic_repo_texts("OTHER=1\n")
    monkeypatch.setattr(gate, "read", lambda p: texts[p])

    texts["CONTRIBUTING.md"] = (
        "## Local setup\n\n"
        "**Environment:** install dependencies and run the backend.\n"
    )
    neither = gate.check_contributing_env_path()
    assert neither and "FAIL contributing-env-path" in neither[0]

    # Pre-#1062 branch still fires: naming `.env.example` without
    # `backend/.env` (the directory-less copy drift) fails as before.
    texts["CONTRIBUTING.md"] = "**Environment:** copy `.env.example` only.\n"
    example_only = gate.check_contributing_env_path()
    assert example_only and "FAIL contributing-env-path" in example_only[0]

    texts["CONTRIBUTING.md"] = (
        "**Environment:** copy `.env.example` to `backend/.env`.\n"
    )
    assert gate.check_contributing_env_path() == []


def test_g06_parsing_strategy_active_divergence_still_fails(monkeypatch):
    """An active non-default value without a default-naming comment fails."""
    gate = _load_gate_module()
    texts = _synthetic_repo_texts(
        'DOCUMENT_PARSING_STRATEGY=fast\n'
    )
    monkeypatch.setattr(gate, "read", lambda p: texts[p])
    failures = gate.check_parsing_strategy()
    assert failures and "fast" in failures[0]


def test_g06_allowed_extensions_parser_accepts_annotated_declaration(monkeypatch):
    """The PRODUCTION parser reads the default set from both declaration forms.

    Exercises scripts/check_g06_setup_docs.py::check_allowed_extensions
    (via monkeypatched repo reads) rather than re-declaring its regex, so a
    production parser change can fail this test (PRR-016). The plain
    ``set[str]`` form is the pre-#734 shape; the
    ``Annotated[set[str], NoDecode]`` form is what #734 ships.
    """
    gate = _load_gate_module()
    template_value = ".txt,.md,.pdf,.pptx,.png,.jpg"
    literal_body = '".txt",\n".md",\n".pdf",\n".pptx",\n'
    for decl_form in (
        "allowed_extensions: set[str] = {%s}\n| set(RASTER_IMAGE_EXTENSIONS)\n"
        % literal_body,
        "allowed_extensions: Annotated[set[str], NoDecode] = {%s}\n"
        "| set(RASTER_IMAGE_EXTENSIONS)\n" % literal_body,
    ):
        texts = _synthetic_repo_texts(
            f"ALLOWED_EXTENSIONS={template_value}\n",
            config_text=decl_form,
        )
        monkeypatch.setattr(gate, "read", lambda p: texts[p])
        assert gate.check_allowed_extensions() == [], (
            f"production parser must accept declaration: {decl_form[:60]}..."
        )

    # Symmetric direction (PRR-010): a template value the code default does
    # not include widens the effective allowlist — the gate must flag it.
    texts = _synthetic_repo_texts(
        f"ALLOWED_EXTENSIONS={template_value},.exe\n",
    )
    monkeypatch.setattr(gate, "read", lambda p: texts[p])
    failures = gate.check_allowed_extensions()
    assert failures and any("unexpected .exe" in line for line in failures)
