"""Repo-wide AST escape guard for XML-tagged prompt interpolations (#711).

Structural guardrail for the RC-07 escape/boundary defect class: every
f-string interpolation of the shape ``<tag>{expr}</tag>`` under ``app/``
must either be a direct call to an escape helper or be listed in the
ALLOWED table below with (a) a human-readable justification and (b) a
``paired_escape`` regex that is re-verified against the module source on
every run — so an allowlisted "pre-escaped local" stops being allowlisted
the moment its upstream escape call disappears or is renamed.

RED against the pre-#711 tree (the unescaped ``{prior_assistant_clipped}``
in query_transformer.py offended); GREEN on the fixed tree.
"""

import ast
import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

_TAG_OPENER = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>$")

_ESCAPE_CALL_NAMES = {"_xml_escape", "_escape", "_header_escape", "escape"}

# (module-relative path, tag name, expression source) -> (justification,
# paired_escape regex that MUST still match the module source).
ALLOWED = {
    (
        "services/agentic_tools.py",
        "source_passages",
        "escaped_snippet",
    ): (
        "pre-escaped local: escaped_snippet = xml.sax.saxutils.escape(raw_snippet)",
        re.compile(r"escaped_snippet\s*=\s*xml\.sax\.saxutils\.escape\("),
    ),
    (
        "services/agentic_tools.py",
        "user_query",
        "escaped_text",
    ): (
        "pre-escaped local: escaped_text = xml.sax.saxutils.escape(str(text))",
        re.compile(r"escaped_text\s*=\s*xml\.sax\.saxutils\.escape\("),
    ),
    (
        "services/contextual_chunking.py",
        "document",
        "doc_view",
    ): (
        "pre-escaped view: chunk_view/doc_view derive from _xml_escape(...) text",
        re.compile(r"chunk_view\s*=\s*_xml_escape\("),
    ),
    (
        "services/contextual_chunking.py",
        "document",
        "chunk_view",
    ): (
        "pre-escaped view: chunk_view = _xml_escape(chunk_text)[:...]",
        re.compile(r"chunk_view\s*=\s*_xml_escape\("),
    ),
    (
        "services/multimodal_prompts.py",
        "page_number",
        "int(page_number)",
    ): (
        "int() conversion: a page number cannot carry markup",
        re.compile(r"<page_number>\{int\(page_number\)\}"),
    ),
}


def _is_escape_call(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Call) and (
        (isinstance(expr.func, ast.Name) and expr.func.id in _ESCAPE_CALL_NAMES)
        or (
            isinstance(expr.func,
                       ast.Attribute) and expr.func.attr in _ESCAPE_CALL_NAMES
        )
    )


def _unescaped_tagged_interpolations(source: str):
    """Yield (lineno, tag, expr_source) for unescaped <tag>{expr}</tag> sites."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr):
            continue
        parts = node.values
        for i, part in enumerate(parts):
            if not isinstance(part, ast.FormattedValue):
                continue
            prev_part = parts[i - 1] if i > 0 else None
            next_part = parts[i + 1] if i + 1 < len(parts) else None
            if not (
                isinstance(prev_part, ast.Constant)
                and isinstance(prev_part.value, str)
            ):
                continue
            if not (
                isinstance(next_part, ast.Constant)
                and isinstance(next_part.value, str)
            ):
                continue
            opener = _TAG_OPENER.search(prev_part.value)
            if opener is None:
                continue
            if not next_part.value.startswith(f"</{opener.group(1)}>"):
                continue
            if _is_escape_call(part.value):
                continue
            yield node.lineno, opener.group(1), ast.unparse(part.value)


def test_no_unescaped_tagged_prompt_interpolations_under_app():
    """Every <tag>{expr}</tag> under app/ is escaped or verified-allowlisted."""
    failures = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        for lineno, tag, expr in _unescaped_tagged_interpolations(source):
            key = (rel, tag, expr)
            allowed = ALLOWED.get(key)
            if allowed is not None:
                justification, paired_escape = allowed
                if not paired_escape.search(source):
                    failures.append(
                        f"{rel}:{lineno} <{tag}>{{{expr}}}: allowlisted as "
                        f'"{justification}" but the paired escape pattern '
                        f"{paired_escape.pattern} no longer matches the module"
                    )
                continue
            failures.append(f"{rel}:{lineno} <{tag}>{{{expr}}}: not escaped")
    assert not failures, (
        "unescaped XML-tagged prompt interpolation(s) found under app/ "
        "(escape them or extend ALLOWED with a verified paired_escape):\n  "
        + "\n  ".join(failures)
    )
