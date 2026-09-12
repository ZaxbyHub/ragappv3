"""Issue #258 acceptance checks — nightly full-dependency tier (AC12/AC13).

The PR-gate CI stubs the heavy parsers (conftest stubs unstructured/pyarrow;
requirements-ci.txt excludes them), so the real document pipeline never runs
in CI. The amendment (C30+E16 / ENH-007 + legacy-10) requires a scheduled
tier that installs the full parser stack and ingests committed binary
fixtures. These nodes pin that contract:

* AC12 — ``.github/workflows/nightly.yml`` exists, triggers on ``schedule``,
  and has a job whose install steps pull the real parser stack
  (``unstructured[all-docs]``, directly or via a requirements file that
  contains it) and whose test steps run the real-parser fixture set
  (a run command referencing ``real_docs`` or the ``real_parser`` marker).
* AC12 — the committed binary fixture set exists under
  ``backend/tests/fixtures/real_docs/`` (PDF with a text table, DOCX, XLSX,
  PPTX, Markdown; each <10KB; structurally valid: PDF header/xref, zip
  integrity + required members). These fixtures are an author-time
  deliverable of the acceptance-check phase — the parsers that prove
  extraction run in the nightly tier, not here.
* AC12 — a parser bake-off benchmark harness is committed
  (``scripts/parser_bakeoff.py`` or tests marked/labelled ``bakeoff``).
* AC13 — the nightly tier runs the FULL backend suite (a pytest run over the
  whole ``tests/`` tree, beyond the PR-gate's targeted subset).

Each node prints an ``AC12``/``AC13 CHECK: PASS|FAIL`` sentinel under ``-s``.
"""

import zipfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
NIGHTLY = REPO / ".github/workflows/nightly.yml"
FIXTURE_DIR = REPO / "backend/tests/fixtures/real_docs"
MAX_FIXTURE_BYTES = 10 * 1024


def _load_nightly() -> tuple[str, dict]:
    assert NIGHTLY.is_file(), (
        ".github/workflows/nightly.yml must exist: the nightly full-dependency "
        "tier (real parsers + committed fixtures + full suite) is the "
        "ENH-007/legacy-10 deliverable"
    )
    text = NIGHTLY.read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def _run_commands(workflow: dict) -> list[str]:
    commands: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps", []):
            run = step.get("run")
            if run:
                commands.append(run)
    return commands


def _requirements_texts_referenced(commands: list[str]) -> list[str]:
    """Content of every backend requirements file referenced by a run step.

    Workflow commands run from either the repo root (``backend/requirements.txt``)
    or the backend dir (``requirements.txt``); both spellings are resolved.
    """
    texts: list[str] = []
    for command in commands:
        for token in command.split():
            if "requirements" not in token or not token.endswith(".txt"):
                continue
            relative = token.lstrip("./")
            candidates = (
                REPO / relative,
                REPO / "backend" / relative.removeprefix("backend/"),
            )
            for candidate in candidates:
                if candidate.is_file():
                    texts.append(candidate.read_text(encoding="utf-8"))
                    break
    return texts


# ── AC12: workflow + schedule + parser stack + fixture test set ─────────────


def test_ac12_nightly_workflow_exists_with_schedule():
    try:
        text, workflow = _load_nightly()
        triggers = workflow.get("on") or workflow.get(True) or {}
        print(f"triggers: {sorted(triggers)}")
        assert "schedule" in triggers, "nightly.yml must trigger on: schedule"
    except Exception:
        print("AC12 CHECK: FAIL")
        raise
    print("AC12 CHECK: PASS")


def test_ac12_nightly_job_installs_full_parser_stack():
    try:
        _, workflow = _load_nightly()
        commands = _run_commands(workflow)
        requirements_texts = _requirements_texts_referenced(commands)
        print(f"run steps: {len(commands)}; requirements files: {len(requirements_texts)}")
        direct = any("unstructured[all-docs]" in command for command in commands)
        via_requirements = any(
            "unstructured[all-docs]" in text for text in requirements_texts
        )
        assert direct or via_requirements, (
            "the nightly tier must install the full parser stack: a run step "
            "references unstructured[all-docs] directly, or installs a backend "
            "requirements file that contains it (requirements.txt does; "
            "requirements-ci.txt does not)"
        )
    except Exception:
        print("AC12 CHECK: FAIL")
        raise
    print("AC12 CHECK: PASS")


def test_ac12_nightly_runs_real_parser_fixture_set():
    try:
        _, workflow = _load_nightly()
        commands = _run_commands(workflow)
        print(f"run steps: {[c.splitlines()[0] for c in commands]}")
        assert any(
            ("pytest" in command and ("real_docs" in command or "real_parser" in command))
            for command in commands
        ), (
            "the nightly tier must run the real-parser fixture test set "
            "(a pytest run referencing real_docs or the real_parser marker)"
        )
    except Exception:
        print("AC12 CHECK: FAIL")
        raise
    print("AC12 CHECK: PASS")


# ── AC13: nightly full backend suite ────────────────────────────────────────


def test_ac13_nightly_runs_full_backend_suite():
    try:
        _, workflow = _load_nightly()
        commands = _run_commands(workflow)
        full_suite = [
            command
            for command in commands
            if "pytest" in command and "tests/" in command
        ]
        print(f"full-suite candidates: {[c.splitlines()[0] for c in full_suite]}")
        assert full_suite, (
            "the nightly tier must run the FULL backend suite over tests/ "
            "(legacy-10: scheduled full-suite CI beyond the PR targeted subset)"
        )
    except Exception:
        print("AC13 CHECK: FAIL")
        raise
    print("AC13 CHECK: PASS")


# ── AC12: committed binary fixtures ─────────────────────────────────────────


def test_ac12_real_docs_fixtures_committed():
    try:
        expected = {
            "sample_table.pdf": "pdf",
            "sample_table.docx": "docx",
            "sample_table.xlsx": "xlsx",
            "sample_table.pptx": "pptx",
            "markdown_notes.md": "md",
        }
        for name, kind in expected.items():
            path = FIXTURE_DIR / name
            assert path.is_file(), f"committed fixture missing: {path}"
            size = path.stat().st_size
            print(f"{name}: {size} bytes ({kind})")
            assert 0 < size < MAX_FIXTURE_BYTES, f"{name} must stay under 10KB"
            if kind == "pdf":
                blob = path.read_bytes()
                assert blob.startswith(b"%PDF-"), f"{name}: missing PDF header"
                assert blob.rstrip().endswith(b"%%EOF"), f"{name}: missing %%EOF"
                # 4 rows x 3 columns of text-showing operators = the table.
                assert blob.count(b" Tj") >= 12, f"{name}: table text ops missing"
            elif kind == "md":
                text = path.read_text(encoding="utf-8")
                assert "|" in text and text.strip(), f"{name}: empty or tableless"
            else:
                required = {
                    "docx": ["word/document.xml"],
                    "xlsx": ["xl/workbook.xml", "xl/worksheets/sheet1.xml"],
                    "pptx": ["ppt/presentation.xml", "ppt/slides/slide1.xml"],
                }[kind]
                with zipfile.ZipFile(path) as archive:
                    assert archive.testzip() is None, f"{name}: corrupt zip member"
                    names = archive.namelist()
                    missing = [m for m in required if m not in names]
                    assert not missing, f"{name}: missing members {missing}"
    except Exception:
        print("AC12 CHECK: FAIL")
        raise
    print("AC12 CHECK: PASS")


# ── AC12: parser bake-off benchmark harness ────────────────────────────────


def test_ac12_parser_bakeoff_harness_committed():
    try:
        script = REPO / "scripts" / "parser_bakeoff.py"
        script_exists = script.is_file()
        marked_tests = [
            path.relative_to(REPO).as_posix()
            for path in (REPO / "backend" / "tests").rglob("*.py")
            if not path.name.startswith("test_issue258")
            and "bakeoff" in path.read_text(encoding="utf-8", errors="replace").lower()
        ]
        print(f"scripts/parser_bakeoff.py exists: {script_exists}; labelled tests: {marked_tests}")
        assert script_exists or marked_tests, (
            "a parser bake-off benchmark harness (Docling vs unstructured-hi_res "
            "vs marker on the same fixtures) must be committed: "
            "scripts/parser_bakeoff.py or tests marked/labelled bakeoff"
        )
    except Exception:
        print("AC12 CHECK: FAIL")
        raise
    print("AC12 CHECK: PASS")
