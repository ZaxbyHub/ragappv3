"""Issue #258 acceptance checks — INSTALLATION.md vs the shipped app (E2).

DOC-001 (Node pins 2-3 majors behind), DOC-002 (zero-arg ``init_db()`` that
raises TypeError at runtime) and DOC-003 (compose recipe referencing undefined
services / unpulled models / non-existent Dockerfiles) were all maintained by
eyeball. The mechanical gate is ``scripts/check_installation_doc.py`` (a
deliverable of this issue; stdlib-only so the Quality-contracts CI job can run
it). These nodes wire it up and add the two layers the script deliberately
leaves to the backend environment:

* AC15/AC18 — the script must exit 0: version mentions match the runtime
  contract's allowed set, snippet arity, compose service graph, model pulls
  and Dockerfile paths all validated deterministically.
* AC16 — the documented ``python -c`` ``init_db`` call is validated with
  ``inspect.signature`` against the REAL ``app.models.database.init_db``
  imported in the backend test environment (conftest stubs heavy deps).
* AC17 — the compose recipe block must be genuine YAML (PyYAML — a locked
  test dependency here, unavailable to the stdlib-only script) and every
  service it references via depends_on or env URLs must be defined.

Each node prints an ``AC<n> CHECK: PASS`` / ``AC<n> CHECK: FAIL`` sentinel
under ``-s``.
"""

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import check_installation_doc as doc_check  # noqa: E402


def test_ac15_installation_doc_script_passes():
    """DOC-001 + AC18 deterministic gate: scripts/check_installation_doc.py."""
    try:
        argv = [sys.executable, str(REPO / "scripts" / "check_installation_doc.py")]
        print(f"argv: {argv}")
        result = subprocess.run(
            argv, cwd=str(REPO), capture_output=True, text=True, timeout=60
        )
        print(f"exit: {result.returncode}")
        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"  {line}")
        assert result.returncode == 0, (
            "scripts/check_installation_doc.py must pass: INSTALLATION.md "
            "versions, snippet arity, compose service graph, model pulls and "
            "Dockerfile paths must match the shipped app"
        )
    except Exception:
        print("AC15 CHECK: FAIL")
        raise
    print("AC15 CHECK: PASS")


def test_ac16_documented_init_db_call_matches_real_signature():
    """DOC-002: inspect-validated arity against the real app function."""
    try:
        from app.models import database

        signature = inspect.signature(database.init_db)
        required = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        print(f"app.models.database.init_db required parameters: {required}")

        doc = doc_check.read_doc()
        checked = 0
        for line_no, code in doc_check.extract_python_c_snippets(doc):
            if "init_db" not in code:
                continue
            checked += 1
            for node in ast.walk(ast.parse(code)):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != "init_db":
                    continue
                supplied = len(node.args) + len(node.keywords)
                print(f"INSTALLATION.md:{line_no}: init_db called with {supplied} arg(s)")
                assert supplied >= len(required), (
                    f"INSTALLATION.md:{line_no}: documented init_db() call passes "
                    f"{supplied} argument(s) but the real signature requires "
                    f"{required} — the documented command raises TypeError"
                )
        assert checked, "no python -c init_db snippet found to validate"
    except Exception:
        print("AC16 CHECK: FAIL")
        raise
    print("AC16 CHECK: PASS")


def test_ac17_compose_recipe_is_yaml_with_closed_service_graph():
    """DOC-003: real YAML parse + every referenced service is defined."""
    try:
        doc = doc_check.read_doc()
        recipe_text = doc_check.extract_compose_recipe(doc)
        assert recipe_text, "INSTALLATION.md has no fenced ```yaml compose recipe"
        recipe = yaml.safe_load(recipe_text)
        services = set((recipe.get("services") or {}).keys())
        print(f"defined services: {sorted(services)}")

        referenced: set[str] = set()
        for service in recipe.get("services", {}).values():
            depends_on = service.get("depends_on") or []
            if isinstance(depends_on, str):
                depends_on = [depends_on]
            referenced.update(depends_on)
            environment = service.get("environment") or []
            if isinstance(environment, dict):
                environment = [f"{k}={v}" for k, v in environment.items()]
            for entry in environment:
                for host in doc_check.URL_RE.findall(str(entry)):
                    if host not in doc_check.NON_SERVICE_HOSTS and not host.startswith("${"):
                        referenced.add(host)
        print(f"referenced services: {sorted(referenced)}")
        undefined = referenced - services
        assert not undefined, (
            f"compose recipe references undefined services {sorted(undefined)} "
            f"(defined: {sorted(services)}) — the recipe cannot start"
        )
    except Exception:
        print("AC17 CHECK: FAIL")
        raise
    print("AC17 CHECK: PASS")


def test_ac18_configured_models_pulled_and_build_paths_exist():
    """AC18 extraction layer: model pulls + Dockerfile path existence."""
    try:
        doc = doc_check.read_doc()
        pull_findings = doc_check.model_pull_findings(doc)
        for finding in pull_findings:
            print(f"  {finding}")

        recipe_text = doc_check.extract_compose_recipe(doc)
        build_findings = (
            doc_check.compose_build_path_findings(recipe_text) if recipe_text else []
        )
        for finding in build_findings:
            print(f"  {finding}")

        assert not pull_findings, (
            "every CHAT_MODEL/INSTANT_CHAT_MODEL value the doc configures must "
            "appear in an ollama pull instruction in the same doc"
        )
        assert not build_findings, (
            "every dockerfile the compose recipe builds must exist in the repo"
        )
    except Exception:
        print("AC18 CHECK: FAIL")
        raise
    print("AC18 CHECK: PASS")
