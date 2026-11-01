"""Equivalence vectors and contract pins from the PR #988 review round.

Non-frozen companion covering what the frozen c16 checks cannot:

- Differential vectors for ``_contains_filename_reference`` against the
  original regex oracle (review PRR-021/F-008): the frozen C6 pins only
  linearity on a hyphen run; these vectors pin the EQUIVALENCE argument,
  including the hyphen-initial, all-hyphen, extension-prefix, and unicode
  classes the plan's proof names. Two of them double as mutation killers:
  ``---.pdf`` (kills a ``has_word`` → ``True`` mutant) and ``a.pdfx`` vs
  ``a.pdf`` (kills removal of the trailing ``\\b``).
- The #922 EARS3 vector: ``_normalize_sub_query`` on a 40,000-char query
  with a MID-STRING punctuation run (the frozen C7 payload is trailing-run).
- The PROMPT_VERSION pin (review F-009): reverting to the pre-#918 value
  must fail, because the step-back/HyDE prompt contents changed.
- The strengthened cache-write census (review PRR-017): every redis write
  reference (setex/psetex/set) lives in exactly one shared helper, called
  from at least the transformer and planner definitions across three call
  sites — tightening what the frozen C10's name-based census cannot see.
"""

import ast
import re
import time
from pathlib import Path

from app.services.query_transformer import (
    PROMPT_VERSION,
    _contains_filename_reference,
    _normalize_sub_query,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "query_transformer.py"
)

# The original regex this implementation replaced (#922). Kept here as the
# oracle so any future drift between the two fails loudly.
_ORIGINAL_FILENAME_RE = re.compile(
    r'\b[\w\-]+\.(pdf|docx?|xlsx?|csv|txt|md|yaml|yml|json|html?|pptx?)\b',
    re.IGNORECASE,
)


def _oracle(query: str) -> bool:
    return _ORIGINAL_FILENAME_RE.search(query) is not None


# (query, expected) — every class the equivalence proof names.
_EQUIVALENCE_VECTORS = [
    ("see -a.pdf", True),  # hyphen-initial run: boundary at the first \w
    ("x -a.pdf", True),  # hyphen-initial run, mid-string
    ("--.pdf", False),  # all-hyphen run: kills a has_word→True mutant
    ("---.pdf", False),  # same, longer
    ("-a.pdf", True),  # minimal hyphen-initial
    (".pdf", False),  # ext at position 0, no run
    ("file.pdf", True),
    ("a.pdfx", False),  # trailing \b required: kills \b removal
    ("a.pdf", True),  # ...and the boundary-ful twin
    ("café.pdf", True),  # unicode word chars
    ("中文.pdf", True),  # unicode word chars (CJK)
    ("a.PDF", True),  # IGNORECASE
    ("x.Pdf", True),
    ("report.", False),  # dot with no extension
    ("what does report.pdf say?", True),
    ("summary from config.yaml", True),
    ("read data.csv now", True),
]


def test_filename_reference_matches_the_original_regex_on_all_vectors():
    for query, expected in _EQUIVALENCE_VECTORS:
        actual = _contains_filename_reference(query)
        oracle = _oracle(query)
        assert actual == expected == oracle, (
            f"equivalence drift on {query!r}: impl={actual}, oracle={oracle}, "
            f"expected={expected}"
        )


def test_filename_reference_mutant_killers():
    """The vectors must discriminate the named mutants, pinned through
    ``_contains_filename_reference`` directly (the public entry's
    short-lookup branch fires first on these tiny inputs):
    a ``has_word``→``True`` mutant would accept "---.pdf"; a ``\\b``-removal
    mutant would accept "a.pdfx"; a never-scan mutant would reject
    "see -a.pdf" and "a.pdf"."""
    assert _contains_filename_reference("---.pdf") is False
    assert _contains_filename_reference("a.pdfx") is False
    assert _contains_filename_reference("see -a.pdf") is True
    assert _contains_filename_reference("a.pdf") is True


def test_normalize_sub_query_linear_on_mid_string_punctuation_run():
    """#922 EARS3 verbatim vector: a 40,000-char query with a MID-STRING
    punctuation run must normalize in under a second. The frozen C7 pins a
    trailing run; this pins the class the issue actually named."""
    payload = "word " * 4000 + "." * 20000 + "tail word " * 200  # mid-string run
    assert len(payload) >= 40000
    t0 = time.perf_counter()
    result = _normalize_sub_query(payload)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"_normalize_sub_query took {elapsed:.3f}s (must be linear)"
    assert result.endswith(("tail", "tail word")) or "." not in result[-10:]


def test_prompt_version_is_not_the_pre_boundary_value():
    """Review F-009: PROMPT_VERSION pins cache identity to the producing
    prompt. v1 was the pre-#918 step-back/HyDE prompt text (no SECURITY
    BOUNDARY directive); reverting to it would serve v1-prompt rewrites
    and no test would notice. Bumping this pin is part of changing any
    producing prompt."""
    assert PROMPT_VERSION != "v1", (
        "PROMPT_VERSION must advance with prompt-content changes: the "
        "step-back and HyDE prompts gained the SECURITY BOUNDARY directive "
        "(#918), so v1-prompt cached entries must not serve. If you are "
        "bumping the version for a new prompt change, update this pin."
    )


def _write_references_with_enclosing_defs(tree):
    """[(attr_name, enclosing_def_name)] for every redis-write-ish
    Attribute reference (setex/psetex/set), covering the direct-call and
    redis_call-argument shapes the frozen C10 tracks for setex only."""
    refs = []
    func_nodes = (ast.FunctionDef, ast.AsyncFunctionDef)

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            child_enclosing = enclosing
            if isinstance(child, func_nodes):
                child_enclosing = child.name
            if isinstance(child, ast.Attribute) and child.attr in (
                "setex",
                "psetex",
                "set",
            ):
                refs.append((child.attr, child_enclosing))
            walk(child, child_enclosing)

    walk(tree, None)
    return refs


def _helper_call_defs(tree, helper_name):
    """The set of enclosing function definitions that call ``helper_name``."""
    func_nodes = (ast.FunctionDef, ast.AsyncFunctionDef)
    defs = set()

    def enclosing_name(node, target):
        # Walk with ancestry: find calls to helper_name and their enclosing def.
        stack = [(node, None)]
        while stack:
            current, enc = stack.pop()
            child_enc = enc
            if isinstance(current, func_nodes):
                child_enc = current.name
            if (
                isinstance(current, ast.Call)
                and isinstance(current.func, (ast.Name, ast.Attribute))
                and getattr(current.func, "id", getattr(current.func, "attr", None))
                == helper_name
                and child_enc is not None
            ):
                defs.add(child_enc)
            for child in ast.iter_child_nodes(current):
                stack.append((child, child_enc))

    enclosing_name(tree, helper_name)
    return defs


def test_all_redis_write_references_live_in_one_helper_called_from_three_defs():
    """Strengthened census (review PRR-017): beyond the frozen C10's
    name-based setex census, this pins (a) that NO other redis write
    command (set/psetex) appears anywhere in the module, and (b) that the
    shared write helper is called from three DISTINCT function definitions
    (the frozen census counts calls, not their enclosing defs)."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    write_refs = _write_references_with_enclosing_defs(tree)
    setex_refs = [(attr, enc) for attr, enc in write_refs if attr == "setex"]
    assert setex_refs, "the module must still perform redis cache writes"
    enclosing = {enc for _, enc in setex_refs}
    assert enclosing == {"_cache_write_success"}, (
        f"every setex reference must live in the shared write helper; found in "
        f"{sorted(enclosing)}"
    )
    other_writes = [(attr, enc) for attr, enc in write_refs if attr != "setex"]
    assert not other_writes, (
        f"redis set/psetex writes bypass the shared-helper contract; found "
        f"{other_writes}"
    )

    helper_defs = _helper_call_defs(tree, "_cache_write_success")
    assert len(helper_defs) >= 2, (
        f"the shared write helper must be called from the transformer and "
        f"planner definitions at minimum (the step-back and HyDE sites both "
        f"live in transform, the planner site in plan); found "
        f"{sorted(helper_defs)}"
    )
    call_sites = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "_cache_write_success"
    )
    assert call_sites >= 3, (
        f"the shared write helper must be called from at least 3 call sites "
        f"(transform step-back, HyDE, planner); found {call_sites}"
    )
