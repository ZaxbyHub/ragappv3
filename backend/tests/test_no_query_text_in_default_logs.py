"""Structural guards for the no-query-text-at-default-log-level contract (#710).

Two guards, both RED at base and GREEN after the fix:

- Guard A (AST): no ``logger.info/.warning/.error/.exception/.critical`` call
  (nor ``logger.log`` at a constant level >= INFO, or with a non-constant or
  absent level — treated as default-level, fail-safe) anywhere in
  ``backend/app/`` may format a query-derived expression. The receiver must
  evaluate to a logger: a bare name bound to ``logging.getLogger(...)``
  anywhere in the module, an attribute named ``logger``/``_logger``/``LOG``,
  or an inline ``logging.getLogger(...).<level>(...)`` call. Keyword
  arguments are checked too (``JsonFormatter`` serializes ``extra={...}``
  values into the emitted JSON body, so ``logger.info("x", extra={"query":
  q})`` is the same leak in a different channel). The predicate is
  CONTAINMENT-based: a queryish name or attribute ANYWHERE inside any
  positional or keyword argument (including f-string interpolation
  expressions and comprehension elements) fails the guard, except inside
  ``len(...)`` calls — count-only lines are the blessed pattern. RED at base:
  the six #710 sites format ``query[:60]``/``sq[:60]``/``[sq[:40] ...]`` at
  INFO/WARNING. GREEN after the fix.

  Known limits of this scan (the enforcement for the trace log path is Guard C
  below plus the allowlist in ``rag_trace.py``, not this scan): text that
  reaches the call through a value pre-built in an earlier statement, a dict
  lookup, ``getattr(...)``, or an exception object's ``str`` is not detected,
  and the name set is a fixed enumeration — an identifier holding user text
  under a name not in it is not detected.

- Guard B (allowlist parity): every ``RAGTrace`` field is classified in
  exactly one of ``_LOG_SAFE_FIELDS`` (logged verbatim) or
  ``_REDACTED_FIELDS`` (summarized), and ``to_dict()`` serializes exactly the
  dataclass fields. RED at base (the projection had no classification at
  all); GREEN after the fix — and a future field addition fails loudly here
  until it is classified, so it can never silently leak or silently vanish.

False-positive policy for the generic names ``text``/``q`` (kept in the set
because they are how future ingestion code will name user text): a hit is
resolved by (a) logging a ``len(...)``/count instead, (b) renaming the
variable when it is NOT user-derived, or (c) demoting the detail line to
``logger.debug``. There is intentionally NO suppression comment or skip
list — the guard fails closed. The attribute set deliberately excludes
``.text``: it flags provider-response logging (e.g. embeddings.py
``response.text`` error bodies), which is tracked separately in issue #903,
not this contract.
"""

import ast
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.rag_trace import _LOG_SAFE_FIELDS, _REDACTED_FIELDS, RAGTrace

# Names that hold user/LLM-derived query text in this codebase. Deliberately
# broad (see module docstring for the false-positive policy). Every entry was
# collision-checked against every default-level log call in backend/app before
# being added (2026-10-14: `user_prompt`, `query_text`, `retrieval_query_text`,
# `rewritten`, `hyde_passage`, `original_query_text` each had 0 hits today).
_QUERYISH_NAMES = frozenset(
    {
        "query",
        "user_input",
        "user_query",
        "retrieval_query",
        "retrieval_query_text",
        "sq",
        "sub_query",
        "sub_queries",
        "sub_queries_failed",
        "failed_sub_queries",
        "q",
        "user_text",
        "text",
        "original_query",
        "question",
        # Built from the raw query / its LLM rewrite; none is logged at a
        # default level today, which is exactly why the guard needs them.
        "user_prompt",
        "query_text",
        "rewritten",
        "hyde_passage",
        "original_query_text",
        # C15 residuals (2026-10-19, #908/#909): `variant_type` holds the
        # RAW sub-query string on the multi-sub-query orchestrator path
        # (rag_engine `_execute_retrieval`) even though the name suggests a
        # label — the label-only loops now use `variant_label`. `title`
        # holds the LLM summary of the session's first chat message in
        # chat.py `_auto_name_session`. Both were collision-checked against
        # every default-level log call in backend/app after this fix's
        # redactions/renames (zero remaining hits).
        "variant_type",
        "title",
    }
)

# Attribute names whose value is user-derived text (``x.query``,
# ``request.user_input`` …). ``.text`` is deliberately excluded — see the
# module docstring.
_QUERYISH_ATTRS = frozenset(
    {
        "query",
        "user_input",
        "user_query",
        "user_text",
        "original_query",
        "question",
    }
)

# Names a module can bind a logger to. Anything else calling ``.info(...)``
# (e.g. ``trace.log()``) is not a logging site.
_LOGGER_BINDING_NAMES = frozenset({"logger", "logging", "log", "_logger", "_log", "LOG"})


def _is_get_logger_call(node: ast.AST) -> bool:
    """True for a ``logging.getLogger(...)`` call (inline receiver form)."""
    func = node.func if isinstance(node, ast.Call) else None
    if not isinstance(func, ast.Attribute) or func.attr != "getLogger":
        return False
    return isinstance(func.value, ast.Name) and func.value.id in ("logging", "log")


def _module_logger_names(tree: ast.AST) -> set:
    """Module-level/local names bound to a logger object, so ``LOG.info(...)``
    and ``self._logger.info(...)`` are recognized as logging sites too."""
    names = set(_LOGGER_BINDING_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_get_logger_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
    return names


def _is_logger_receiver(node: ast.AST, tree: ast.AST | None = None) -> bool:
    """True when ``node`` evaluates to a logger.

    Recognizes the bare-name form (``logger``/``LOG``/anything bound to
    ``logging.getLogger(...)`` anywhere in the module), the ``.logger``
    attribute form, the inline ``logging.getLogger(...).warning(...)`` form
    (live at backend/app/api/routes/vaults.py:578), and
    ``self._logger``-style attributes whose final name is bound in the module.
    """
    if _is_get_logger_call(node):
        return True
    if isinstance(node, ast.Name):
        return tree is not None and node.id in _module_logger_names(tree)
    if isinstance(node, ast.Attribute):
        if node.attr == "logger":
            return True
        return tree is not None and node.attr in _module_logger_names(tree)
    return False


def _contains_queryish_text(node: ast.AST) -> str | None:
    """Return a human-readable description of the first query-derived
    expression found anywhere in ``node``, or None. ``len(...)`` subtrees are
    skipped: count-only logging is the blessed pattern."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        return None
    if isinstance(node, ast.Name) and node.id in _QUERYISH_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in _QUERYISH_ATTRS:
        return f".{node.attr}"
    for child in ast.iter_child_nodes(node):
        hit = _contains_queryish_text(child)
        if hit is not None:
            return hit
    return None


def _guarded_level_ok(call: ast.Call) -> bool:
    """For ``logger.log(...)``: guarded when the level is a constant >= INFO
    (20) OR is not a constant (fail-safe: treated as default level).

    The level may arrive positionally or as the ``level=`` keyword — the
    keyword-only form used to skip the whole call, which was a real bypass.
    An absent level on ``logger.log`` is also treated as guarded.
    """
    level = call.args[0] if call.args else None
    if level is None:
        for kw in call.keywords:
            if kw.arg == "level":
                level = kw.value
                break
    if level is None:
        return True  # no level at all: cannot prove it is below INFO
    if isinstance(level, ast.Constant) and isinstance(level.value, int):
        return level.value >= 20
    return True


def _iter_app_files(app_dir=None):
    """Yield TRACKED ``backend/app/*.py`` files only (#906 AC4).

    The walk is scoped to ``git ls-files`` output so untracked/generated
    trees under ``backend/app`` (e.g. graphify output) cannot change the
    guard's verdict between developer machines and CI. If git is genuinely
    unavailable (no repository context), the walk falls back to the full
    ``rglob`` — the pre-#906 behavior — because failing the whole guard on
    a tooling gap would be worse than the narrower scope; every environment
    that ships this repo (CI, dev clones) has git.

    ``app_dir`` is injectable so the tracked-scope test can exercise the real
    walk against a throwaway git repo instead of planting a probe inside the
    live source tree (where it raced other xdist workers' ``rglob`` walks —
    swarm-pr-review PRR-001).
    """
    backend_dir = Path(__file__).resolve().parent.parent
    root_dir = backend_dir if app_dir is None else Path(app_dir).resolve().parent
    app_root = backend_dir / "app" if app_dir is None else Path(app_dir).resolve()
    try:
        listed = subprocess.run(
            ["git", "-C", str(root_dir), "ls-files", "--", app_root.name],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        for path in sorted(app_root.rglob("*.py")):
            yield path
        return
    for rel in sorted(line for line in listed.stdout.splitlines() if line.endswith(".py")):
        yield root_dir / rel


_scan_logger = logging.getLogger(__name__)


def _find_query_text_at_default_level(app_dir=None):
    """Yield ``file:line: description`` for every guarded logging call in
    backend/app that formats query-derived text.

    #906 AC3: unreadable or unparseable files (BOM, non-UTF-8 bytes —
    ``read_text`` raises ``UnicodeDecodeError``, a ``ValueError`` — or any
    ``SyntaxError``) are skipped with a counted INFO note instead of
    failing the scan with an opaque error (conftest.py's census-loop
    pattern).

    ``app_dir`` is forwarded to :func:`_iter_app_files` so the tracked-scope
    test can scan a throwaway git repo (swarm-pr-review PRR-001)."""
    skipped = 0
    backend_dir = (
        Path(__file__).resolve().parent.parent if app_dir is None
        else Path(app_dir).resolve().parent
    )
    for path in _iter_app_files(app_dir):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, ValueError, SyntaxError):
            skipped += 1
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not _is_logger_receiver(func.value, tree):
                continue
            if func.attr in ("info", "warning", "error", "exception", "critical"):
                pass  # always default-or-higher
            elif func.attr == "log":
                if not _guarded_level_ok(node):
                    continue
            else:
                continue
            # Skip the level argument of logger.log(level, ...) itself; it is
            # not user text. Everything else (format string + positional args
            # + keyword values, including extra= which JsonFormatter emits and
            # the msg= keyword) is checked.
            args = list(node.args[1:] if func.attr == "log" and len(node.args) > 1 else node.args)
            args.extend(kw.value for kw in node.keywords if not (func.attr == "log" and kw.arg == "level"))
            for arg in args:
                hit = _contains_queryish_text(arg)
                if hit is not None:
                    try:
                        rel = os.path.relpath(path, backend_dir)
                    except ValueError:
                        # Windows cross-drive relpath (planted file on
                        # another mount): fall back to the path string.
                        rel = str(path)
                    yield (
                        f"{rel}"
                        f":{node.lineno}: logger.{func.attr} formats query-derived '{hit}'"
                    )
                    break
    if skipped:
        _scan_logger.info(
            "no-query-text guard: skipped %d unreadable/unparseable file(s)", skipped
        )


def test_no_query_text_formatted_at_default_log_level():
    """Guard A: no default-level log call in backend/app formats query text.

    RED at base (six #710 sites), GREEN after the fix; catches f-string,
    percent-format and ``extra=`` variants of the same leak via the containment
    walk over every argument expression.

    Implemented scope (do not read this as universal coverage): the receiver
    must evaluate to a logger (bare name bound to ``logging.getLogger(...)``
    anywhere in the module, an attribute named ``logger``/``_logger``/``LOG``,
    or an inline ``logging.getLogger(...).<level>(...)``), and the text must
    reach the call through an argument expression — a query string pre-built
    into another variable in an earlier statement, a ``dict`` value, or an
    exception object's ``str`` is NOT detected. That is why the enforcement
    for the log projection itself is the allowlist in ``rag_trace.py`` and the
    emission assertion below, not this scan.
    """
    violations = list(_find_query_text_at_default_level())
    assert not violations, (
        "default-level log calls formatting query-derived text "
        "(violates the #287/#710 no-raw-query-at-default-level contract):\n"
        + "\n".join(violations)
    )


def _populated_trace() -> RAGTrace:
    """A trace with every redacted field populated, so emission can be checked
    on the redaction branch rather than the empty passthrough."""
    trace = RAGTrace(original_query="what is the policy for C05QUERY")
    trace.transformed_queries = ["C05QUERY rephrased once", "C05QUERY rephrased twice"]
    trace.variants_dropped = ["C05QUERY dropped variant"]
    trace.wiki_query = "C05QUERY wiki probe"
    trace.followup_rewrite = "C05QUERY rewritten followup"
    trace.query_plan = ["C05QUERY facet one", "C05QUERY facet two"]
    trace.unverifiable_claims = ["the C05UC claim has no citation"]
    return trace


def test_log_projection_emits_every_classified_field():
    """Guard C: the log projection emits exactly the classified field set.

    Closes the silent-vanish hole: the parity guard above compares dataclass
    fields, ``to_dict()`` keys and the two classification sets, but never called
    ``to_log_dict()`` — so deleting one ``loggable[...] = ...`` summary left
    every test green while the field disappeared from the operator's log.
    Also pins each redacted field's value as summary-shaped, so a text field
    classified into ``_LOG_SAFE_FIELDS`` cannot silently emit verbatim.
    """
    loggable = _populated_trace().to_log_dict()
    classified = set(_LOG_SAFE_FIELDS) | set(_REDACTED_FIELDS)
    assert set(loggable) == classified, (
        "to_log_dict() must emit exactly the classified fields. "
        f"missing: {sorted(classified - set(loggable))}; "
        f"unexpected: {sorted(set(loggable) - classified)}"
    )
    for field in _REDACTED_FIELDS:
        value = loggable[field]
        assert isinstance(value, str) and value.startswith("<redacted "), (
            f"redacted field '{field}' must be emitted as a non-reversible "
            f"summary, got {value!r} — a verbatim value here is a #710 leak"
        )
    # Non-text fields keep flowing with their real values.
    trace = _populated_trace()
    trace.fused_hits = 7
    trace.rerank_status = "applied"
    emitted = trace.to_log_dict()
    assert emitted["fused_hits"] == 7 and emitted["rerank_status"] == "applied"
    assert emitted["original_query"].startswith("<redacted len=")
    assert "C05" not in str(emitted)


def test_empty_and_none_redacted_fields_pass_through_unchanged():
    """Guard C (edge): falsy user-text fields stay falsy, and empty lists
    still produce a zero count rather than vanishing from the log dict."""
    empty = RAGTrace()
    emitted = empty.to_log_dict()
    assert emitted["wiki_query"] == ""
    assert emitted["followup_rewrite"] is None
    assert emitted["transformed_queries"] == "<redacted count=0>"
    assert emitted["query_plan"] == "<redacted count=0>"
    assert emitted["unverifiable_claims"] == "<redacted count=0>"
    assert emitted["original_query"].startswith("<redacted len=0 hash=")

    none_query = RAGTrace()
    none_query.original_query = ""  # empty, not None: len()/hash still safe
    assert none_query.to_log_dict()["original_query"].startswith("<redacted len=0")


def test_trace_fields_fully_classified_for_logging():
    """Guard B: every RAGTrace field is classified safe-or-redacted exactly once.

    RED at base (no classification existed), GREEN after the fix. A future
    field addition fails here until it joins _LOG_SAFE_FIELDS (logged verbatim)
    or _REDACTED_FIELDS (summarized) — and to_dict() must serialize exactly
    the dataclass fields, so a field cannot vanish from both surfaces either.
    ``test_log_projection_emits_every_classified_field`` is the companion that
    covers what this one cannot: the emitted log dict itself.
    """
    trace_field_names = {f.name for f in dataclass_fields(RAGTrace)}
    to_dict_keys = set(RAGTrace().to_dict().keys())
    assert trace_field_names == to_dict_keys, (
        "RAGTrace fields and to_dict() keys diverge — every field must be "
        f"serialized. fields-only: {sorted(trace_field_names - to_dict_keys)}; "
        f"dict-only: {sorted(to_dict_keys - trace_field_names)}"
    )
    assert not (_LOG_SAFE_FIELDS & _REDACTED_FIELDS), (
        "a field cannot be both safe and redacted: "
        f"{sorted(_LOG_SAFE_FIELDS & _REDACTED_FIELDS)}"
    )
    assert to_dict_keys == set(_LOG_SAFE_FIELDS) | set(_REDACTED_FIELDS), (
        "every RAGTrace field must be classified in exactly one of "
        "_LOG_SAFE_FIELDS/_REDACTED_FIELDS for the log projection. "
        f"unclassified: {sorted(to_dict_keys - _LOG_SAFE_FIELDS - _REDACTED_FIELDS)}"
    )
    assert set(_REDACTED_FIELDS) == {
        "original_query",
        "transformed_queries",
        "variants_dropped",
        "wiki_query",
        "followup_rewrite",
        "query_plan",
        "unverifiable_claims",
    }, "the redacted set is pinned: it is exactly the user-derived text fields"


# ======================================================================
# Guard-robustness checks for issue #906 (added by the c15 trace's
# acceptance-check author). These pin the #906 hardening contract:
#
# - test_guard_scanned_a_real_file_set  (#906 AC1 / trace C14): a
#   scanned-file floor so the guard cannot pass vacuously, with the
#   failure message naming the expected minimum.
# - test_detector_rejects_synthetic_query_leak (#906 AC2 / trace C15):
#   a planted violation proves the shipped detector still detects.
# - test_guard_skips_unreadable_files  (#906 AC3 / trace C16):
#   BOM / non-UTF-8 / unparseable files are skipped with a counted
#   note instead of an opaque error. RED at base, GREEN after the fix.
# - test_guard_verdict_ignores_untracked_trees (#906 AC4 / trace C17):
#   an untracked generated tree under backend/app cannot change the
#   verdict. RED at base, GREEN after the fix.
# ======================================================================

# backend/app held 145 tracked .py files at fcc5144e (2026-10-19). The floor
# is deliberately far below the live count so routine refactors do not trip
# it while a moved scan root or broken glob (0-ish files) always does.
_MIN_GUARDED_FILES = 50


def _assert_scanned_floor(paths, label: str) -> list:
    """Fail, naming the expected minimum, when a scan inspects too little."""
    files = list(paths)
    assert len(files) >= _MIN_GUARDED_FILES, (
        f"{label}: the #710 guard scanned only {len(files)} files under "
        f"backend/app — a real run must inspect at least {_MIN_GUARDED_FILES} "
        "(backend/app held 145 at fcc5144e). If the scan root moved or the "
        "glob broke, fix the scan; do not lower this floor."
    )
    return files


def test_guard_scanned_a_real_file_set():
    """#906 AC1: the guard must prove it scanned a real file set.

    GREEN while backend/app exists (the live tree far exceeds the floor);
    RED if the scan root is moved/renamed or the glob stops matching —
    previously the guard reported zero violations and passed while
    inspecting nothing. The failure-message path is exercised against an
    empty scan so the message naming the expected minimum is itself pinned
    (mirrors test_a06_contract_guards.py's guarded-count floor).
    """
    files = _assert_scanned_floor(_iter_app_files(), "guard scan")
    assert any(path.name == "main.py" for path in files), (
        "the scan must include backend/app/main.py; a scan that misses it is "
        "scanning the wrong root"
    )

    with pytest.raises(AssertionError) as excinfo:
        _assert_scanned_floor([], "empty probe")
    assert str(_MIN_GUARDED_FILES) in str(excinfo.value), (
        "the floor's failure message must name the expected minimum so a "
        "vacuous scan fails diagnosably"
    )


_PLANTED_LEAK_SOURCE = (
    "import logging\n"
    "logger = logging.getLogger(__name__)\n"
    "\n"
    "def _planted_leak(query):\n"
    '    logger.info("q=%s", query)\n'
)


def test_detector_rejects_synthetic_query_leak(tmp_path):
    """#906 AC2: the shipped detector flags a planted query-text leak.

    A detector that can only ever pass proves nothing. A synthetic module
    carrying ``logger.info("q=%s", query)`` is fed through the REAL scan
    pipeline (receiver recognition + containment walk + yield), and the
    scan must flag exactly it (mirrors test_n03_guardrails.py's planted
    census violation). GREEN at base and after the fix; RED if a refactor
    of the containment walk silently stops detecting anything.
    """
    planted = tmp_path / "planted_leak.py"
    planted.write_text(_PLANTED_LEAK_SOURCE, encoding="utf-8")

    with mock.patch(
        f"{__name__}._iter_app_files", return_value=iter([planted])
    ):
        violations = list(_find_query_text_at_default_level())

    assert len(violations) == 1, (
        "the detector must flag exactly the planted query-text log call; "
        f"got: {violations}"
    )
    assert "planted_leak.py" in violations[0], (
        f"the flagged site must be the planted file; got: {violations}"
    )


class _ListHandler(logging.Handler):
    """Collect every record at or above its level; no assertion semantics."""

    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture_root_records(level: int = logging.INFO):
    """Capture INFO+ records from ANY logger (skip notes may be logged on
    this module's own logger, which has no configured handlers)."""
    root = logging.getLogger()
    handler = _ListHandler(level)
    previous_level = root.level
    root.setLevel(level)
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_guard_skips_unreadable_files(tmp_path):
    """#906 AC3: unreadable/unparseable files are skipped with a counted note.

    A UTF-8 BOM (ast.parse rejects the BOM char), a non-UTF-8 byte sequence
    (read_text raises UnicodeDecodeError, which is a ValueError — NOT an
    OSError), and plain broken syntax are planted next to a good file that
    itself carries a violation. The scan must complete (RED at base: the
    unguarded read_text/ast.parse raise), still flag the good file's
    violation (skipping must not turn the scan vacuous), and surface the
    skipped files as a counted note — an INFO-or-higher log record
    mentioning "skip" — so unscannable files are never silently ignored.
    GREEN after the fix.
    """
    good = tmp_path / "good.py"
    good.write_text(_PLANTED_LEAK_SOURCE, encoding="utf-8")
    bom = tmp_path / "bom.py"
    bom.write_bytes("\ufeff".encode("utf-8") + _PLANTED_LEAK_SOURCE.encode("utf-8"))
    latin1 = tmp_path / "latin1.py"
    latin1.write_bytes("x = '\x80\x81'\n".encode("latin-1"))
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n    pass\n", encoding="utf-8")
    files = [good, bom, latin1, broken]

    with _capture_root_records(logging.INFO) as captured:
        with mock.patch(
            f"{__name__}._iter_app_files", return_value=iter(files)
        ):
            violations = list(_find_query_text_at_default_level())

    assert len(violations) == 1 and "good.py" in violations[0], (
        "the scan must complete AND still flag the readable file's planted "
        f"violation; got: {violations}"
    )
    skip_notes = [
        r.getMessage() for r in captured.records if "skip" in r.getMessage().lower()
    ]
    assert skip_notes, (
        "unreadable/unparseable files must be reported as a counted note "
        "(an INFO-or-higher log record mentioning 'skip'), never silently "
        "ignored (#906 AC3)"
    )


def test_guard_verdict_ignores_untracked_trees(tmp_path):
    """#906 AC4: untracked/generated trees cannot change the guard verdict.

    A throwaway git repo is built under ``tmp_path``: one tracked source file,
    then an UNTRACKED generated file carrying a planted violation. The real
    walk (tracked-only) must ignore it. RED at base (an rglob walk judges every
    ``*.py`` it finds, so the generated file flips the verdict); GREEN after
    the fix scopes the scan to tracked sources.

    The fixture lives entirely outside the repository: the previous version
    planted a fixed-path probe directory under the live ``backend/app`` tree,
    where its ``finally``-only cleanup raced other xdist workers' unguarded
    ``rglob`` walks and could fail unrelated guards (swarm-pr-review PRR-001).
    """
    repo = tmp_path / "repo"
    app_dir = repo / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q"], check=True, timeout=30
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "app/__init__.py"], check=True, timeout=30
    )
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "base"], check=True, timeout=30, env=env
    )
    # Untracked: committed history does not contain it, so a tracked-only walk
    # must never enumerate it.
    (app_dir / "zz_generated_probe.py").write_text(_PLANTED_LEAK_SOURCE, encoding="utf-8")

    scanned = list(_iter_app_files(app_dir))
    assert any(path.name == "__init__.py" for path in scanned), scanned
    assert not any(path.name == "zz_generated_probe.py" for path in scanned), (
        f"the tracked-only walk enumerated an untracked file: {scanned}"
    )

    violations = list(_find_query_text_at_default_level(app_dir))
    assert violations == [], (
        "the guard verdict changed because of an UNTRACKED generated file "
        f"({violations}): the scan must judge tracked sources only, so "
        "developer machines and CI agree (#906 AC4)"
    )
