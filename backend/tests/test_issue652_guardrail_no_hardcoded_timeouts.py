"""Guardrail for issue #652's defect class: provider-client factories must not
default their HTTP read timeout to a numeric literal.

Pre-#652, ``create_thinking_client``/``create_editorial_client`` hardcoded
``timeout=300.0`` and ``create_instant_client`` ``timeout=120.0``, so no
operator could raise the read timeout for a slow model deployment and Draft
Room compose died at the standards stage against the always-reasoning server.
The fix resolves the default from ``settings.*_request_timeout_seconds``; this
guardrail AST-scans ``llm_client.py`` and fails if any provider-client factory
regresses to a numeric-literal default.

Demonstrated RED on the pre-fix tree (defaults were ``300.0``/``300.0``/
``120.0`` constants) and GREEN after the fix (all three default to ``None``
and resolve from settings).
"""

import ast
from pathlib import Path

FACTORIES = (
    "create_thinking_client",
    "create_editorial_client",
    "create_instant_client",
)


def _factory_timeout_defaults():
    """Map factory name -> AST default expression of its ``timeout`` param."""
    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "llm_client.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    defaults = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) or isinstance(node, ast.FunctionDef):
            if node.name not in FACTORIES:
                continue
            for arg, default in _pair_params_defaults(node.args):
                if arg == "timeout":
                    defaults[node.name] = default
    return defaults


def _pair_params_defaults(args):
    positional = args.posonlyargs + args.args
    pad = len(positional) - len(args.defaults)
    for arg, default in zip(positional, [None] * pad + list(args.defaults)):
        yield arg.arg, default
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        yield arg.arg, default


def test_guardrail_no_hardcoded_factory_timeout_defaults():
    defaults = _factory_timeout_defaults()
    assert set(defaults) == set(FACTORIES), "factory set drifted"
    for name, default in defaults.items():
        assert default is not None, f"{name}: timeout parameter has no default"
        assert isinstance(default, ast.Constant) and default.value is None, (
            f"{name}: timeout defaults to {ast.dump(default)} — numeric-literal "
            "factory timeouts are the #652 defect class; resolve from "
            "settings.*_request_timeout_seconds instead (None + settings fallback)"
        )


def test_guardrail_no_literal_timeout_in_factory_bodies():
    """A regression that moves a numeric literal from the signature into the
    factory BODY (``LLMClient(timeout=300.0)``) would slip past the signature
    scan above (#654 review F-06 mutation probe); scan the call sites too."""
    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "llm_client.py"
    ).read_text(encoding="utf-8")
    offenders = _literal_timeout_call_sites(ast.parse(source))
    assert not offenders, (
        f"numeric-literal LLMClient(timeout=...) call sites: {offenders} — "
        "resolve from settings.*_request_timeout_seconds instead"
    )


def test_guardrail_detector_flags_literal_call_sites():
    """Self-test for the body scanner: it must flag the exact regression shape
    F-06's mutation probe described (literal moved into a constructor call)."""
    snippet = (
        "def create_broken_client(timeout=None):\n"
        "    return LLMClient(timeout=300.0)\n"
        "def create_ok_client(timeout=None):\n"
        "    return LLMClient(timeout=timeout)\n"
    )
    assert _literal_timeout_call_sites(ast.parse(snippet)) == ["line 2"]
    assert _literal_timeout_call_sites(ast.parse("LLMClient(timeout=None)\n")) == []


def _literal_timeout_call_sites(tree):
    """Return 'line N' entries for LLMClient(...) calls whose ``timeout=``
    keyword is a numeric constant."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "LLMClient"):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "timeout"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, (int, float))
            ):
                hits.append(f"line {node.lineno}")
    return hits


def test_guardrail_factories_reference_settings_timeouts():
    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "llm_client.py"
    ).read_text(encoding="utf-8")
    for setting in (
        "settings.thinking_request_timeout_seconds",
        "settings.editorial_request_timeout_seconds",
        "settings.instant_request_timeout_seconds",
    ):
        assert setting in source, f"{setting} no longer read by the factories"
