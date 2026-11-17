"""Table-ownership census guardrail for issue #790 (Workstream N PR 6 of 6).

``draft_store.py``'s docstring claimed a single-writer contract that was
never true. The honest map, frozen by this census (counts re-derived at base
3e00fbf6 and re-asserted below): ``DraftStore`` is the default owner of all
six Draft Room tables, and exactly three co-owners write them directly —

* ``api/routes/draft_room.py`` — 8 sites: cross-table transactional helpers
  (compile enqueue/retry on ``drafts``/``draft_jobs``, apply-finding and
  manual revisions on ``draft_revisions``/``draft_findings``/``drafts``,
  and the Ready transition on ``drafts``);
* ``services/draft_job_processor.py`` — 7 sites: worker-lifecycle writes
  (``drafts.status`` fail/requeue, retry ``draft_jobs`` rows,
  ``draft_inputs.parse_status``);
* ``services/draft_pipeline.py`` — 11 sites: compile-time writes
  (``draft_jobs`` progress columns, revision publication including the
  ``is_current`` flip, ``draft_events`` inserts, ``drafts.status`` settle).

The scan is AST-based, not a line regex, because the audit that found this
defect proved a regex census fails in BOTH directions: it under-counts SQL
assembled by concatenation or f-strings, and it over-counts prose — the
``routes/vaults.py:770`` comment "no separate DELETE FROM drafts needed" is
matched by the audit's grep but is not a write. Parsing the module and folding
string expressions (implicit concatenation, f-strings, ``+`` chains,
``str.format`` templates) into a static SQL string eliminates both errors:
comments never become AST nodes, and concatenated fragments are joined before
matching. Every verb+table pair in the folded string is examined (``finditer``,
not first-match), and the verb grammar covers SQLite's write forms —
``INSERT [OR ...] INTO``, ``REPLACE INTO``, ``UPDATE [OR ...]`` and
``DELETE FROM`` — with the table accepted in every static spelling SQLite
itself writes: bare, double-quoted, backtick-quoted, bracket-quoted, and
schema-qualified (``main.drafts``), with comments allowed between verb and
table. Boundary, documented: the census sees only writes whose verb AND table
are static SQL text (a dynamic ``f"UPDATE {table} ..."`` or a table passed to
``.format()`` is inherently invisible to static analysis and is not a false
positive either — the dynamic marker never matches a table name). A write
against ``drafts``, ``draft_jobs``, ``draft_revisions``,
``draft_events``, ``draft_inputs`` or ``draft_findings`` in any module outside
the owner set fails this census. Migrating the 26 co-owner writes into
``DraftStore`` is deliberately out of scope (issue #790); this guard exists
so the map cannot silently regress to "single owner" or grow a fifth writer.

Precision boundary (issue #926), with each limitation's trigger named:
a verb+table match counts only at a live statement position — comments are
stripped quote-aware (single/double/backtick/bracket quoting all suppress
the markers; a single-line ``/* */`` comment is replaced by a space so the
surrounding tokens do not join), the verb must start a statement
(start-of-string, ``;``, ``(`` or ``)`` behind it — ``)`` admits
CTE-prefixed writes), single-quoted SQL literal content is BLANKED (a
literal is data: SQL text held in a string value cannot forge a statement,
while double-quote/backtick/bracket identifier content — the table-name
spellings — stays visible), and the prose colon after the table rejects
sentence noise. Disclosed limitations the grammar cannot close: a SQL
constant defined in one module and executed in another stays invisible
(cross-module name resolution is out of scope — the name map is
module-local); ``job_lease.py`` writes ``draft_jobs`` through a
constructor-supplied table attribute, unreachable to any static fold
(declared co-owner in ``draft_store.py``'s map instead); directory
symlinked DIRECTORIES under the scan root are not traversed (symlinked
``.py`` files ARE scanned, matching base ``rglob`` behavior); a
``PermissionError`` on a directory aborts the walk
(fail closed); any name bound inside a function (parameter, assignment,
``for``/``with`` target, walrus, import alias, nested def/class/lambda, or
``except ... as``) shadows a module constant and
resolves to nothing (fail toward missing, never the wrong value — feedback
FB-04); the ``)`` anchor admits a write that follows a parenthetical in
prose (over-counting — the census fails loudly, never silently); a dynamic
(``\\x00``) placeholder inside a SQL comment or single-quoted literal can
diverge from runtime tokenization, so a write revealed by the runtime value
can be hidden from the fold; and an UNTERMINATED single quote blanks the
remainder of a folded string (FB-11 — the guard is an accidental-regression
detector, not a hostile-source analyzer). In a multi-generator comprehension
each generator's iterable sees only targets bound by earlier generators,
and its guards see its own target too (the exact PEP 289 prefix rule);
this precision boundary is disclosed here.
"""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

_BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

#: The complete ownership map. ``draft_store.py`` is the default owner; the
#: three co-owners are declared for their specific write families above.
OWNER_MODULES = frozenset(
    {
        "services/draft_store.py",
        "api/routes/draft_room.py",
        "services/draft_job_processor.py",
        "services/draft_pipeline.py",
    }
)

#: The six tables whose writers must all live in OWNER_MODULES.
CENSUS_TABLES = frozenset(
    {
        "drafts",
        "draft_jobs",
        "draft_revisions",
        "draft_events",
        "draft_inputs",
        "draft_findings",
    }
)

#: Co-owner site counts at base 3e00fbf6 (issue #790 audit provenance: ENH-13
#: review, 8 + 7 + 11 = 26). EXACT pins, not minimums (feedback FB-05): the
#: precision work moved no real site, so an additive write inside a declared
#: owner must be reviewed and this map updated deliberately rather than
#: passing silently. The pins exist so a scanner regression that stops seeing
#: writes fails loudly instead of passing vacuously.
_KNOWN_MINIMUMS = {
    "api/routes/draft_room.py": 8,
    "services/draft_job_processor.py": 7,
    "services/draft_pipeline.py": 11,
}

#: ``DraftStore`` is the DEFAULT owner of these tables (its module docstring
#: says so), so its own write surface is pinned too. Without this the store
#: could be gutted of every six-table write and the whole census would stay
#: green — the outside-store total excludes the store by construction. 46 sites
#: measured at the current head (48 at base 3e00fbf6); the margin absorbs
#: legitimate refactors (feedback FB-05 corrected the stale 48 figure).
_STORE_MINIMUM = 40

#: SQLite write grammar. The verb/table separator is an ATOMIC group: without
#: it the ``(?:\s|/\*.*?\*/|--[^\n]*)+`` outer ``+`` can partition a run of
#: dashes 2^n ways and backtrack exponentially on a failing tail (measured
#: 9.7s at 40 dashes, unbounded beyond; the atomic form answers in ~1e-6s at
#: the same input). One intended behaviour change comes with it: a table named
#: on the SAME line as a line comment after the verb (``UPDATE -- c drafts``)
#: is no longer matched, because the atomic group cannot give the comment back.
#: That is the SQL-correct reading of a line comment and the shape has no real
#: occurrence in ``backend/app``; the newline form still matches.
_VERB_RE = re.compile(
    r"\b("
    r"(?:INSERT|REPLACE)(?:\s+OR\s+(?:IGNORE|REPLACE|ABORT|FAIL|ROLLBACK))?\s+INTO"
    r"|DELETE\s+FROM"
    r"|UPDATE(?:\s+OR\s+(?:IGNORE|REPLACE|ABORT|FAIL|ROLLBACK))?"
    r")"
    r"(?>\s+|/\*.*?\*/|--[^\n]*)+\s*"
    r'("[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|[A-Za-z_][A-Za-z0-9_]*)'
    r"(?:\s*\.\s*"
    r'("[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|[A-Za-z_][A-Za-z0-9_]*)'
    r")?"  # the schema qualifier is optional; the table itself is not
    ,
    re.IGNORECASE | re.DOTALL,
)


def _canonical_table(raw: str) -> str:
    """Normalize a captured table reference to its bare lower-case name.

    Accepts the identifier forms SQLite itself writes: bare, double-quoted,
    backtick-quoted, bracket-quoted, and schema-qualified (``main.drafts``);
    the census keys on the final component.
    """
    parts = re.split(r"\s*\.\s*", raw.strip())
    last = parts[-1].strip()
    if last[:1] in ('"', "`", "[") and len(last) >= 2:
        last = last[1:-1]
    return last.lower()

#: Placeholder for dynamic (non-static) parts of a folded SQL string; chosen
#: so it can never match the identifier capture group above.
_DYNAMIC = "\x00"


WriteSite = namedtuple("WriteSite", ["rel_path", "lineno", "verb", "table"])


def _fold(
    node: ast.AST,
    names: dict[str, tuple[str, int]] | None = None,
    local_assigned: frozenset[str] = frozenset(),
    used_names: set[str] | None = None,
) -> str | None:
    """Best-effort static value of a string-bearing expression.

    Dynamic parts collapse to ``_DYNAMIC`` so a verb followed by a dynamic
    table reference cannot match a real table name (no false positive), while
    a verb and table both inside the static prefix still match (no false
    negative for folded/concatenated/f-string SQL).

    Issue #926 blind spot 4: with ``names`` (a module-level constant map of
    ``name -> (folded value, value-node id)``), an ``ast.Name`` operand
    resolves to its module constant — one level, module scope only. A name
    that is also assigned anywhere inside the ENCLOSING function scope
    (``local_assigned``) resolves to nothing: local shadowing fails toward
    missing rather than binding the wrong value. ``used_names`` records which
    module constants were consumed, so their assignment-site constants can be
    suppressed (no assignment-site + use-site double count).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            else:
                parts.append(_DYNAMIC)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold(node.left, names, local_assigned, used_names)
        right = _fold(node.right, names, local_assigned, used_names)
        if left is None or right is None:
            return None
        return left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _fold(node.func.value, names, local_assigned, used_names)
    if isinstance(node, ast.Name) and names is not None:
        # Only a LOAD counts as a use of the constant — an assignment
        # TARGET (Store context) defines it, it does not consume it.
        if not isinstance(node.ctx, ast.Load):
            return None
        if node.id in local_assigned:
            return None
        if node.id in names:
            if used_names is not None:
                used_names.add(node.id)
            return names[node.id][0]
    return None


def _strip_sql_comments(text: str) -> str:
    """R1 (issue #926 blind spot 2): strip SQL comments, quote-aware.

    ``--`` line comments and ``/* */`` block comments are removed ONLY
    outside quoted tokens; the scanner tracks all four SQLite quoting
    styles (single-quoted strings, double-quoted identifiers, backtick,
    and bracket quoting), so a ``--`` inside any quoted token is string
    text, not a comment boundary (``"SELECT '--'; UPDATE drafts ..."``
    keeps its live write). A block comment that spanned lines leaves a
    newline behind so statement anchors survive; a single-line block
    comment is replaced by a space so the surrounding tokens do not join
    (``UPDATE/* c */drafts`` must not become ``UPDATEdrafts``).

    Implementation-review round 3: single-quoted tokens are SQL string
    LITERALS (data) — their CONTENT is blanked (same length, benign
    filler), so SQL text held as data (``SELECT '; UPDATE drafts ...'``)
    cannot forge a live statement anchor or a verb+table match.
    Double-quoted, backtick, and bracket tokens are IDENTIFIERS (the
    table-name spellings) — their content stays visible.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is None:
            if ch in ("'", '"', "`"):
                quote = ch
                out.append(ch)
                i += 1
            elif ch == "[":
                quote = "]"
                out.append(ch)
                i += 1
            elif ch == "-" and i + 1 < n and text[i + 1] == "-":
                j = text.find("\n", i)
                i = n if j == -1 else j
            elif ch == "/" and i + 1 < n and text[i + 1] == "*":
                j = text.find("*/", i + 2)
                if j == -1:
                    i = n
                else:
                    if "\n" in text[i : j + 2]:
                        # A multi-line block comment still separates
                        # statements; keep the newline as the anchor.
                        out.append("\n")
                    else:
                        # Single-line block comment: emit a SPACE so the
                        # surrounding tokens do not join
                        # (``UPDATE/* c */drafts`` must not become
                        # ``UPDATEdrafts`` — review round 2, finding 1).
                        out.append(" ")
                    i = j + 2
            else:
                out.append(ch)
                i += 1
        else:
            if quote == "]":
                # Bracket quoting is an IDENTIFIER form — keep the content
                # (the census must capture ``[draft_inputs]`` tables).
                out.append(ch)
                if ch == "]":
                    quote = None
            elif ch == quote:
                out.append(ch)
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            elif quote == "'":
                # Implementation-review round 3: a single-quoted token is a
                # SQL string LITERAL (data). Blank its content (same length,
                # benign filler) so SQL text held as data
                # ("SELECT '; UPDATE drafts SET status = 1'") cannot forge a
                # live statement anchor or a verb+table match. Double-quoted
                # and backtick tokens are IDENTIFIERS — their content stays
                # (table names must remain capturable).
                out.append(" ")
            else:
                out.append(ch)
            i += 1
    return "".join(out)


def _match_is_live_statement(stripped: str, match: re.Match) -> bool:
    """R2 + R3 (issue #926 blind spots 1-2): SQL-statement context gates.

    R2 — statement-start anchor: the verb must begin a SQL statement; the
    nearest preceding non-whitespace character must be start-of-string,
    ``;``, ``(``, or ``)`` (whitespace after the anchor is tolerated, so
    ``";   UPDATE drafts ..."`` matches, and a CTE-prefixed
    ``WITH x AS (SELECT 1) UPDATE drafts ...`` is admitted — review round 2,
    finding 2).

    R3 — prose-separator gate: the character following the captured table
    reference must not be ``:`` — the published prose shape
    ``"delete from drafts: constraint failed"`` starts a sentence with the
    verb but continues with prose. The rule is deliberately conservative
    (colon-only): valid SQLite continuations (``SELECT``, ``DEFAULT
    VALUES``, ``RETURNING``, ``WHERE``, ``SET``, ``VALUES``, ...) all pass,
    trading nothing for the prose false positive.
    """
    k = match.start() - 1
    while k >= 0 and stripped[k] in " \t\r\n":
        k -= 1
    if k >= 0 and stripped[k] not in ";()":
        return False
    e = match.end()
    n = len(stripped)
    while e < n and stripped[e] in " \t\r\n":
        e += 1
    if e < n and stripped[e] == ":":
        return False
    return True


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Identity of every bare string statement in ``tree``.

    Docstrings are documentation, not SQL; without this exclusion a
    docstring describing an UPDATE would count as a write. Issue #926
    blind spot 3: the exclusion covers EVERY bare string ``ast.Expr``
    statement — PEP 258 attribute docstrings after another statement and
    no-op string statements under ``if``/``try`` included — not only
    first-statement docstrings.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            ids.add(id(node.value))
    return ids


def _module_constants(tree: ast.Module) -> dict[str, tuple[str, int]]:
    """Module-level constants: ``name -> (folded value, value-node id)``.

    Only literal-composed module-level assignments participate; a name
    whose value is not foldable without names is simply absent (fail toward
    missing).
    """
    consts: dict[str, tuple[str, int]] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        folded = _fold(value)
        if folded is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = (folded, id(value))
    return consts


def _function_assigned_names(tree: ast.Module) -> dict[int, frozenset[str]]:
    """Per-function sets of names bound in THAT function's own scope.

    Feedback (Copilot r4593838474, Stage B FB-04): the original collector
    saw only Assign/AnnAssign/AugAssign targets, so parameters, loop/with
    targets, walrus, import aliases, destructuring,
    ``except ... as`` and match-pattern captures that shadowed a module SQL
    constant were resolved to the wrong value. All binding forms are now
    collected, including walrus bindings in nested default expressions (which
    Python evaluates in the enclosing scope) — but ONLY in the function's own
    lexical scope: a comprehension target owns only the comprehension scope
    (PEP 289); a nested function's parameter does not shadow its enclosing
    function's name, so descending into nested bodies (as ``ast.walk(fn)`` did)
    wrongly suppressed genuine outer-scope uses. Nested def/class/lambda NAMES
    are bound in the enclosing scope; their parameters/body bindings are not.
    """

    def bound_target(node: ast.AST) -> set[str]:
        names: set[str] = set()
        if isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                names |= bound_target(elt)
        elif isinstance(node, ast.Starred):
            names |= bound_target(node.value)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        return names

    def bound_pattern(pattern: ast.AST | None) -> set[str]:
        """All capture names in a match pattern, including nested wrappers
        (MatchClass / MatchOr / MatchSequence / MatchMapping / MatchAs)."""
        names: set[str] = set()
        if pattern is None:
            return names
        if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name:
            names.add(pattern.name)
        elif isinstance(pattern, ast.MatchMapping) and pattern.rest:
            names.add(pattern.rest)
        for child in ast.iter_child_nodes(pattern):
            names |= bound_pattern(child)
        return names

    def arg_names(args: ast.arguments) -> set[str]:
        return {
            arg.arg
            for arg in (
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                args.vararg,
                args.kwarg,
            )
            if arg is not None
        }

    def own_scope(fn: ast.AST):
        """Yield nodes in fn's body without descending into nested scopes."""
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            node = stack.pop()
            yield node
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                continue
            stack.extend(ast.iter_child_nodes(node))

    def bindings_from(nodes) -> set[str]:
        """Collect every binding form from nodes in one lexical scope."""
        names: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    names |= bound_target(target)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                names |= bound_target(node.target)
            elif isinstance(node, (ast.NamedExpr, ast.For, ast.AsyncFor)):
                names |= bound_target(node.target)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        names |= bound_target(item.optional_vars)
            elif isinstance(node, ast.comprehension):
                # PEP 289: a comprehension target has its OWN scope in
                # Python 3 — it does not shadow a name in the enclosing
                # function. Registered separately (keyed by node id) so
                # enter_scope can apply it INSIDE the comprehension only.
                pass
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)  # only the name binds here
                # Defaults, decorators, annotations and class bases are all
                # evaluated in the ENCLOSING scope; a walrus in any of them
                # binds there (Stage B FB-04).
                exprs: list[ast.expr] = []
                if isinstance(node, ast.ClassDef):
                    exprs += [*node.bases, *(kw.value for kw in node.keywords)]
                else:
                    exprs += [
                        *node.args.defaults,
                        *(d for d in node.args.kw_defaults if d is not None),
                    ]
                    for arg in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                        node.args.vararg,
                        node.args.kwarg,
                    ):
                        if arg is not None and arg.annotation is not None:
                            exprs.append(arg.annotation)
                    if node.returns is not None:
                        exprs.append(node.returns)
                exprs += [*node.decorator_list]
                for expr in exprs:
                    for sub in ast.walk(expr):
                        if isinstance(sub, ast.NamedExpr):
                            names |= bound_target(sub.target)
            elif isinstance(node, ast.Lambda):
                # Same enclosing-scope rule for lambda defaults.
                for default in (
                    *node.args.defaults,
                    *(d for d in node.args.kw_defaults if d is not None),
                ):
                    for sub in ast.walk(default):
                        if isinstance(sub, ast.NamedExpr):
                            names |= bound_target(sub.target)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.match_case):
                names |= bound_pattern(node.pattern)
        return names

    assigned: dict[int, frozenset[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assigned[id(node)] = frozenset(
                arg_names(node.args) | bindings_from(own_scope(node))
            )
        elif isinstance(node, ast.Lambda):
            # Lambda args PLUS bindings inside its body (a walrus in a lambda
            # body shadows the module constant too — Stage B FB-04).
            assigned[id(node)] = frozenset(
                arg_names(node.args) | bindings_from(own_scope(node))
            )
        elif isinstance(node, ast.comprehension):
            # Comprehension targets own ONLY the comprehension's scope
            # (PEP 289); registered here so enter_scope applies them there.
            assigned[id(node)] = frozenset(bound_target(node.target))
    return assigned


def scan_tree(root: Path) -> list[WriteSite]:
    """Census ``root`` for write expressions against the six tables.

    Fails closed: a file that cannot be decoded or parsed raises
    ``AssertionError`` naming it, because an unreadable file could hide
    writers. Decoding is ``utf-8-sig`` so a Windows-authored BOM (which
    CPython imports happily) is not misreported as a syntax error.

    Grammar (issue #926): a write is caught when its verb and table are
    static SQL text at a live statement position — R1 strips comments
    quote-aware, R2 anchors the verb to a statement start, R3 rejects the
    prose colon. Disclosed limitations, each with its trigger: a SQL
    constant defined in one module and executed in another is invisible
    (cross-module name resolution is out of scope); ``job_lease.py`` writes
    ``draft_jobs`` through a constructor-supplied table attribute the fold
    cannot see; symlinked directories under the scan root are not traversed
    (symlinked ``.py`` files are still scanned); a ``PermissionError`` on a
    directory aborts the walk (fail closed); any function-local binding
    (parameter, assignment, for/with/walrus/comprehension/import/nested-def)
    shadowing a module constant resolves to nothing (fail toward missing);
    a dynamic placeholder inside a comment/literal may hide a write the
    runtime value reveals, and an unterminated single quote blanks the rest
    of a folded string (FB-11 — accidental-regression scope only).
    """
    def iter_py_files(root: Path):
        """Explicit walk: an unreadable directory raises, it is not skipped.

        Python 3.11's ``Path.rglob`` *suppresses* directory PermissionError
        (its recursive selector catches and returns), which would silently
        omit a subtree that could hide a writer. Closeout FB-20: walk
        explicitly so the documented fail-closed contract is real. Symlinked
        directories remain untraversed (the disclosed limitation).
        """
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(directory.iterdir())
            except PermissionError as exc:
                raise AssertionError(
                    f"census cannot read directory {directory}: {exc}"
                ) from exc
            for entry in entries:
                if entry.is_symlink():
                    # Base Path.rglob("*.py") returned symlinked .py FILES;
                    # preserve that. Only symlinked DIRECTORIES stay
                    # untraversed (the disclosed limitation).
                    if entry.suffix == ".py" and entry.is_file():
                        yield entry
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file() and entry.suffix == ".py":
                    yield entry

    sites: list[WriteSite] = []
    for path in sorted(iter_py_files(root)):
        try:
            source = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"census cannot decode {path}: {exc}") from exc
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise AssertionError(f"census cannot parse {path}: {exc}") from exc
        consts = _module_constants(tree)
        func_assigned = _function_assigned_names(tree)
        skip_docstrings = _docstring_ids(tree)

        # Pass A: discover which module constants are consumed by folded
        # uses, so their assignment-site constants can be suppressed (a
        # whole-SQL constant executed via its name counts exactly once).
        used: set[str] = set()

        def fold(
            node: ast.AST, local: frozenset[str] = frozenset()
        ) -> str | None:
            return _fold(node, consts, local, used)

        def enter_scope(node: ast.AST, local: frozenset[str]) -> frozenset[str]:
            """Extend, never replace, the enclosing scope's shadow set.

            Stage B (FB-04): the previous ``local = func_assigned[...]``
            discarded an outer function's bindings when entering a nested
            function or lambda, so a closure read could resolve a module
            constant the outer scope had shadowed. Union is the safe
            direction (extra shadows only suppress a resolution).
            """
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                return local | func_assigned.get(id(node), frozenset())
            return local

        def child_scopes(node: ast.AST, local: frozenset[str]):
            """Yield (child, child_local) with PEP 289 comprehension scoping.

            A comprehension target is in scope for the element/key/value and
            the generator guards, but NOT for the leftmost iterable — Python
            evaluates that in the enclosing scope. Closeout FB-04.
            """
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                # Defaults, decorators, annotations and class bases execute
                # in the ENCLOSING scope; only the body uses the new scope.
                inner = enter_scope(node, local)
                outer_exprs: set[ast.AST] = set(
                    getattr(node, "decorator_list", [])
                )
                returns = getattr(node, "returns", None)
                if isinstance(node, ast.ClassDef):
                    outer_exprs |= set(node.bases)
                    outer_exprs |= {kw.value for kw in node.keywords}
                else:
                    outer_exprs |= set(node.args.defaults)
                    outer_exprs |= {
                        d for d in node.args.kw_defaults if d is not None
                    }
                    if returns is not None:
                        outer_exprs.add(returns)
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.arguments):
                        for sub in ast.iter_child_nodes(child):
                            if isinstance(sub, ast.arg):
                                for ann in ast.iter_child_nodes(sub):
                                    yield ann, local  # annotation: enclosing scope
                                continue
                            yield sub, (local if sub in outer_exprs else inner)
                    else:
                        yield child, (local if child in outer_exprs else inner)
                return
            if not isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                for child in ast.iter_child_nodes(node):
                    yield child, local
                return
            gens = node.generators
            all_targets: frozenset[str] = frozenset()
            for gen in gens:
                all_targets |= func_assigned.get(id(gen), frozenset())
            for field in ("elt", "key", "value"):
                expr = getattr(node, field, None)
                if expr is not None:
                    yield expr, local | all_targets
            for idx, gen in enumerate(gens):
                previous: frozenset[str] = frozenset()
                for earlier in gens[:idx]:
                    previous |= func_assigned.get(id(earlier), frozenset())
                own = func_assigned.get(id(gen), frozenset())
                # Each generator's ITERABLE is evaluated before its own
                # target binds — only earlier targets are in scope there.
                yield gen.iter, local | previous
                for condition in gen.ifs:
                    yield condition, local | previous | own
            return

        def walk_refs(node: ast.AST, local: frozenset[str]) -> None:
            effective = enter_scope(node, local)
            if isinstance(
                node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call, ast.Name)
            ):
                fold(node, effective)
            for child, child_local in child_scopes(node, local):
                walk_refs(child, child_local)

        walk_refs(tree, frozenset())
        skip = skip_docstrings | {
            consts[name][1] for name in used if name in consts
        }

        def visit(node: ast.AST, local: frozenset[str]) -> None:
            if id(node) in skip:
                return
            effective = enter_scope(node, local)
            folded = None
            if isinstance(
                node,
                (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call, ast.Name),
            ):
                folded = fold(node, effective)
            if folded is not None:
                stripped = _strip_sql_comments(folded)
                matched = False
                for match in _VERB_RE.finditer(stripped):
                    if not _match_is_live_statement(stripped, match):
                        continue
                    raw = match.group(2)
                    if match.group(3):
                        raw = f"{raw}.{match.group(3)}"
                    table = _canonical_table(raw)
                    if table in CENSUS_TABLES:
                        sites.append(
                            WriteSite(
                                path.relative_to(root).as_posix(),
                                getattr(node, "lineno", 0),
                                match.group(1).upper(),
                                table,
                            )
                        )
                        matched = True
                if matched:
                    return  # outermost expression wins; inner parts not re-counted
            for child, child_local in child_scopes(node, local):
                visit(child, child_local)

        visit(tree, frozenset())
    return sites


class TestTableOwnershipCensus(unittest.TestCase):
    def test_census_counts_the_known_co_owner_writes(self):
        sites = scan_tree(_BACKEND_APP)
        by_module: dict[str, int] = {}
        for site in sites:
            by_module[site.rel_path] = by_module.get(site.rel_path, 0) + 1

        unlisted = sorted(
            f"{s.rel_path}:{s.lineno} {s.verb} {s.table}"
            for s in sites
            if s.rel_path not in OWNER_MODULES
        )
        self.assertEqual(
            unlisted,
            [],
            "module(s) outside the ownership map write Draft Room tables "
            f"directly: {unlisted}. Either (a) route the writes through "
            "DraftStore, or (b) the module is legitimate and belongs in "
            "OWNER_MODULES with a matching entry in the draft_store docstring "
            "owner map. If the hit is NOT a write — a documentation string, a "
            "migration/backfill in models/database.py, or example SQL kept in "
            "a string constant — that is a scanner precision bug, not a "
            "missing co-owner; see #926 (issue #790)",
        )

        store_sites = by_module.get("services/draft_store.py", 0)
        self.assertGreaterEqual(
            store_sites,
            _STORE_MINIMUM,
            f"DraftStore is the default owner of the six tables but the census "
            f"sees only {store_sites} write sites there (floor "
            f"{_STORE_MINIMUM}); the outside-store total excludes the store by "
            "construction, so without this pin the store could be gutted of "
            "every six-table write and the census would stay green — if "
            "those writes were legitimately removed, shrink the pin",
        )

        outside_store = [s for s in sites if s.rel_path != "services/draft_store.py"]
        self.assertGreaterEqual(
            len(outside_store),
            26,
            f"census found only {len(outside_store)} co-owner write sites; "
            "the known count at issue #790's base is 26 (8 route + 7 "
            "processor + 11 pipeline) — if writes were legitimately removed, "
            "shrink the pin; if the scanner went blind, fix the scanner",
        )
        for module, exact in _KNOWN_MINIMUMS.items():
            # Plan-critic round 1: pin the co-owner counts EXACTLY (8/7/11),
            # not as floors — precision work must not move real sites in
            # EITHER direction, and a floor cannot detect double-counting
            # inside a declared owner. The store keeps its floor (46 at the
            # 2026-11-16 baseline) because DraftStore is the default owner
            # whose write surface legitimately grows.
            self.assertEqual(
                by_module.get(module, 0),
                exact,
                f"{module} carried exactly {exact} declared write sites at "
                f"issue #790's base but the census now sees "
                f"{by_module.get(module, 0)} — the census precision work "
                "must not move real sites in either direction",
            )

    def test_a_python_comment_is_not_a_write(self):
        """The audit's own false positive, as an executable pin.

        ``routes/vaults.py:770`` carries the comment "draft row purged above
        with no separate DELETE FROM drafts needed", which a line-regex census
        counts. Comments never become AST nodes, so no site may be produced for
        that exact text anywhere. (The earlier ``assertNotIn(
        "api/routes/vaults.py", by_module)`` could only ever fire when the
        ``unlisted`` assertion had already fired, so it was dead weight and a
        permanent ban on a legitimate future co-owner; this pins the property
        the comment-FP fix was actually about.)
        """
        with tempfile.TemporaryDirectory() as tmp:
            comment_only = Path(tmp) / "comment_only.py"
            comment_only.write_text(
                "# draft row purged above with no separate DELETE FROM drafts needed\n"
                "VALUE = 1\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_tree(Path(tmp)), [])

    def test_a_docstring_is_not_a_write(self):
        """Docstring exclusion is a claimed mechanism, so it is pinned."""
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "docstring_writer.py"
            doc.write_text(
                '"""Touches draft_events via UPDATE draft_events, in prose only."""\n'
                "import sqlite3\n\n\n"
                "def noop(conn: sqlite3.Connection) -> None:\n"
                "    return None\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_tree(Path(tmp)), [])

    def test_census_fails_closed_on_an_unparseable_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.py"
            broken.write_text("def oops(:\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as ctx:
                scan_tree(Path(tmp))
        self.assertIn("cannot parse", str(ctx.exception))

    def test_census_fails_closed_on_a_non_utf8_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            latin1 = Path(tmp) / "latin1.py"
            latin1.write_bytes(b"VALUE = 'caf\xe9'\n")
            with self.assertRaises(AssertionError) as ctx:
                scan_tree(Path(tmp))
        self.assertIn("cannot decode", str(ctx.exception))

    def test_census_parses_a_utf8_bom_module(self):
        """``utf-8-sig``, not ``utf-8``: CPython imports a BOM'd file happily,
        so misreading one as a syntax error would red a build over a legal file."""
        with tempfile.TemporaryDirectory() as tmp:
            bom = Path(tmp) / "bom.py"
            bom.write_bytes(
                b"\xef\xbb\xbf"
                b'"""Docs."""\n\n\n'
                b"def touch(conn) -> None:\n"
                b'    conn.execute("UPDATE drafts SET status = 1")\n'
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual([s.table for s in sites], ["drafts"])

    def test_census_flags_an_unlisted_concatenated_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            rogue = Path(tmp) / "rogue_writer.py"
            rogue.write_text(
                "import sqlite3\n\n\n"
                "def touch(conn: sqlite3.Connection) -> None:\n"
                "    conn.execute(\n"
                "        \"UPDATE drafts SET \" + \"status = 'x' WHERE id = 1\"\n"
                "    )\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("rogue_writer.py", "drafts")],
            "a concatenated-SQL write from a module outside the ownership "
            "map must be reported by the census; a miss means the scanner "
            "went blind and test_census_counts_the_known_co_owner_writes is "
            "passing vacuously (issue #790 AC4)",
        )

    def test_census_flags_an_unlisted_f_string_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            rogue = Path(tmp) / "rogue_fstring_writer.py"
            rogue.write_text(
                "import sqlite3\n\n\n"
                "def touch(conn: sqlite3.Connection, status: str) -> None:\n"
                "    conn.execute(\n"
                "        f\"UPDATE draft_jobs SET status = '{status}' WHERE id = 1\"\n"
                "    )\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("rogue_fstring_writer.py", "draft_jobs")],
            "an f-string SQL write from a module outside the ownership map "
            "must be reported by the census (issue #790 AC3's fold-awareness "
            "requirement)",
        )

    def test_census_flags_an_unlisted_or_ignore_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            rogue = Path(tmp) / "rogue_or_ignore_writer.py"
            rogue.write_text(
                "import sqlite3\n\n\n"
                "def seed(conn: sqlite3.Connection) -> None:\n"
                "    conn.execute(\n"
                "        \"INSERT OR IGNORE INTO drafts (id) VALUES (1)\"\n"
                "    )\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("rogue_or_ignore_writer.py", "drafts")],
            "an INSERT OR IGNORE write against a census table from outside "
            "the ownership map must be reported; SQLite OR-modifier and "
            "REPLACE INTO forms are writes too (issue #790 plan-critic "
            "round 1: these idioms are in active repo use)",
        )

    def test_census_sees_every_statement_in_a_multistatement_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            rogue = Path(tmp) / "rogue_batch_writer.py"
            rogue.write_text(
                "import sqlite3\n\n\n"
                "def batch(conn: sqlite3.Connection) -> None:\n"
                "    conn.executescript(\n"
                "        \"UPDATE jobs SET a = 1; UPDATE drafts SET b = 2\"\n"
                "    )\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("rogue_batch_writer.py", "drafts")],
            "a multi-statement SQL string whose FIRST verb targets a "
            "non-census table must not hide its later census-table write; "
            "every verb+table pair in the folded string is examined "
            "(issue #790 plan-critic round 1)",
        )

    def test_census_flags_quoted_and_schema_qualified_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            rogue = Path(tmp) / "rogue_quoted_writer.py"
            rogue.write_text(
                "import sqlite3\n\n\n"
                "def touch(conn: sqlite3.Connection) -> None:\n"
                "    conn.execute('UPDATE \"drafts\" SET status = 1 WHERE id = 1')\n"
                "    conn.execute('UPDATE main.draft_jobs SET status = 1 WHERE id = 1')\n"
                "    conn.execute('DELETE FROM [draft_inputs] WHERE id = 1')\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [
                ("rogue_quoted_writer.py", "drafts"),
                ("rogue_quoted_writer.py", "draft_jobs"),
                ("rogue_quoted_writer.py", "draft_inputs"),
            ],
            "quoted (\"drafts\"), schema-qualified (main.draft_jobs), and "
            "bracket-quoted ([draft_inputs]) table names are static SQL the "
            "census must recognize — the quoted form is what SQLite's own "
            "dump tooling emits (issue #790 implementation review round 1)",
        )


    def test_census_ignores_prose_in_a_string_literal(self):
        """#926 blind spot 1: prose mentioning a write is not a write.

        ``MSG = "... no separate DELETE FROM drafts needed"`` is a note in a
        string literal, not SQL. RED at base (the folded literal matches the
        verb regex with no SQL-statement context requirement), GREEN after
        the fix reports zero sites for it. Per the plan critic's round-1
        revision, every published prose shape is pinned: mid-string prose,
        sentence-initial prose (``"delete from drafts: constraint failed"``),
        label-led prose (``"cleanup: DELETE FROM drafts"``), PEP 258
        attribute docstrings, and bare string statements under ``if``
        (issue #926 blind spots 1 and 3).
        """
        cases = {
            "mid_string_prose": (
                'MSG = "note: the draft row is purged above with '
                'no separate DELETE FROM drafts needed"\n'
            ),
            "sentence_initial_prose": (
                'ERR = "delete from drafts: constraint failed"\n'
            ),
            "label_led_prose": 'NOTE = "cleanup: DELETE FROM drafts"\n',
            "attribute_docstring": (
                "class C:\n"
                "    x = 1\n"
                '    """attr docstring: UPDATE drafts SET title = 1"""\n'
            ),
            "bare_string_under_if": (
                'def f() -> None:\n'
                '    if True:\n'
                '        "DELETE FROM drafts WHERE id = 1"\n'
            ),
        }
        for name, source in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                notes = Path(tmp) / "notes.py"
                notes.write_text(source, encoding="utf-8")
                sites = scan_tree(Path(tmp))
                self.assertEqual(
                    sites,
                    [],
                    "prose_in_literal: a prose string literal mentioning a "
                    f"census table write is not SQL ({name}); the census "
                    f"reported {sites} (issue #926 blind spots 1 and 3)",
                )

    def test_census_ignores_sql_commented_text_in_a_string_literal(self):
        """#926 blind spot 2: text behind a SQL comment is not live SQL.

        ``Q = 'SELECT 1 -- old: DELETE FROM drafts'`` keeps the DELETE only
        inside the comment. RED at base (the regex matches forward from the
        verb and cannot see the ``--`` boundary), GREEN after the fix
        reports zero sites for it. Per the plan critic's round-1 revision:
        block comments strip the same way, and a QUOTED ``'--'`` literal
        must not disable the live write that follows it (quote-aware
        stripping — markers inside single-quoted strings are text).
        """
        with tempfile.TemporaryDirectory() as tmp:
            commented = Path(tmp) / "commented.py"
            commented.write_text(
                "Q = 'SELECT 1 -- old: DELETE FROM drafts'\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            sites,
            [],
            "sql_comment_in_literal: text after a SQL line comment inside a "
            f"string literal is not live SQL; the census reported {sites} "
            "(issue #926 blind spot 2)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            block_commented = Path(tmp) / "blockcommented.py"
            block_commented.write_text(
                'Q = "SELECT 1 /* old: DELETE FROM drafts */"\n',
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            sites,
            [],
            "sql_comment_in_literal: text inside a SQL block comment inside "
            f"a string literal is not live SQL; the census reported {sites} "
            "(issue #926 blind spot 2)",
        )
        # Mutation-probe hardening: comment text must not be able to FORGE a
        # statement anchor. With stripping disabled, the ';' inside the
        # '-- cleanup;' comment would satisfy the R2 anchor and admit the
        # commented-out DELETE — only R1 kills this shape.
        with tempfile.TemporaryDirectory() as tmp:
            forged_anchor = Path(tmp) / "forgedanchor.py"
            forged_anchor.write_text(
                "Q = 'SELECT 1\\n-- cleanup;\\nDELETE FROM drafts'\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            sites,
            [],
            "sql_comment_in_literal: a ';' inside a SQL line comment must "
            "not forge a statement anchor for the commented-out verb; the "
            f"census reported {sites}",
        )
        with tempfile.TemporaryDirectory() as tmp:
            quoted_marker = Path(tmp) / "quotedmarker.py"
            quoted_marker.write_text(
                'Q = "SELECT \'--\'; UPDATE drafts SET title = 1"\n',
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("quotedmarker.py", "drafts")],
            "sql_comment_in_literal: a QUOTED '--' is string text, not a "
            "comment boundary — the live UPDATE after the semicolon must "
            "still be recorded; the census saw "
            f"{[(s.rel_path, s.table) for s in sites]}",
        )
        # Implementation-review round 3: SQL text held as string DATA must
        # not forge a live statement — the quoted-value content is blanked,
        # so the ';' inside the value cannot anchor the embedded verb.
        with tempfile.TemporaryDirectory() as tmp:
            quoted_value = Path(tmp) / "quotedvalue.py"
            quoted_value.write_text(
                'def go(conn) -> None:\n'
                "    conn.execute(\"SELECT '; UPDATE drafts SET status = 1'\")\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            sites,
            [],
            "sql_comment_in_literal: a write embedded in a quoted SQL VALUE "
            "is data, not a live statement; the census reported "
            f"{sites}",
        )
        # ...and a real write AFTER a quoted value stays visible.
        with tempfile.TemporaryDirectory() as tmp:
            after_value = Path(tmp) / "aftervalue.py"
            after_value.write_text(
                'def go(conn) -> None:\n'
                "    conn.execute("
                "\"SELECT 'x'; UPDATE drafts SET status = 1\")\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(x.rel_path, x.table) for x in sites],
            [("aftervalue.py", "drafts")],
            "sql_comment_in_literal: a live write following a quoted value "
            f"must stay visible; the census saw "
            f"{[(x.rel_path, x.table) for x in sites]}",
        )
        # Plan-critic round 2: SQLite double-quoted IDENTIFIERS may contain
        # '--' — stripping there would hide a subsequent live write.
        with tempfile.TemporaryDirectory() as tmp:
            dq_marker = Path(tmp) / "dqmarker.py"
            dq_marker.write_text(
                "Q = 'SELECT 1; UPDATE \"we--ird\".drafts SET title = 1'\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("dqmarker.py", "drafts")],
            "sql_comment_in_literal: a '--' inside a double-quoted "
            "identifier is identifier text, not a comment boundary — the "
            f"live UPDATE must be recorded; the census saw "
            f"{[(s.rel_path, s.table) for s in sites]}",
        )

    def test_census_accepts_valid_continuation_forms_after_the_table(self):
        """Plan-critic round 2: the after-table gate must not trade prose
        false positives for valid-SQL false negatives.

        Every form here is VALID SQLite (verified by execution in the plan
        review) and must be recorded: INSERT ... SELECT, INSERT ... DEFAULT
        VALUES, DELETE ... RETURNING. GREEN after the fix ships the
        conservative colon-only prose gate; at base all three PASS too (the
        base regex matches them) — this row's discrimination lives in the
        prose/comment rows above; these are the no-false-negative pins."""
        # Implementation-review round 2, finding 1: a single-line block
        # comment between verb and table must not JOIN the tokens — the
        # stripper replaces it with a space, and the write stays visible.
        with tempfile.TemporaryDirectory() as tmp:
            block_sep = Path(tmp) / "blocksep.py"
            block_sep.write_text(
                "def go(conn) -> None:\n"
                "    conn.execute(\"UPDATE/* note */drafts SET status = 1\")\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(x.rel_path, x.table) for x in sites],
            [("blocksep.py", "drafts")],
            "valid_sql_continuation: a block comment used as the only "
            "verb/table separator must not hide the write; the census saw "
            f"{[(x.rel_path, x.table) for x in sites]}",
        )
        # Implementation-review round 2, finding 2: a CTE-prefixed write is a
        # live statement — the anchor set admits ')' after the CTE.
        with tempfile.TemporaryDirectory() as tmp:
            cte = Path(tmp) / "cte.py"
            cte.write_text(
                "def go(conn) -> None:\n"
                "    conn.execute("
                "\"WITH x AS (SELECT 1) UPDATE drafts SET status = 1\")\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(x.rel_path, x.table) for x in sites],
            [("cte.py", "drafts")],
            "valid_sql_continuation: a CTE-prefixed write must be recorded; "
            f"the census saw {[(x.rel_path, x.table) for x in sites]}",
        )
        valid_forms = {
            "insert_select": (
                "def go(conn) -> None:\n"
                "    conn.execute('INSERT INTO drafts SELECT * FROM tmp')\n"
            ),
            "default_values": (
                "def go(conn) -> None:\n"
                "    conn.execute('INSERT INTO drafts DEFAULT VALUES')\n"
            ),
            "delete_returning": (
                "def go(conn) -> None:\n"
                "    conn.execute('DELETE FROM drafts RETURNING id')\n"
            ),
        }
        for name, source in valid_forms.items():
            with self.subTest(form=name), tempfile.TemporaryDirectory() as tmp:
                module = Path(tmp) / "validform.py"
                module.write_text(source, encoding="utf-8")
                sites = scan_tree(Path(tmp))
                self.assertEqual(
                    [(s.rel_path, s.table) for s in sites],
                    [("validform.py", "drafts")],
                    "valid_sql_continuation: valid SQLite continuation forms "
                    f"({name}) must never become false negatives; the census "
                    f"saw {[(s.rel_path, s.table) for s in sites]}",
                )

    def test_census_records_a_name_indirection_write(self):
        """#926 blind spot 4: a module-level name feeding a write is seen.

        ``PREFIX + "draft_jobs SET status = 1"`` is a real write against a
        census table assembled across a variable boundary. RED at base
        (``_fold`` gives up on the ``ast.Name`` operand and records zero
        sites), GREEN after the fix records the site. Per the plan critic's
        round-1 revision: a whole-SQL module constant counts exactly ONCE
        (no assignment-site + use-site double count), and a semicolon
        anchor with whitespace before the verb still starts a statement.
        """
        with tempfile.TemporaryDirectory() as tmp:
            indirect = Path(tmp) / "indirect.py"
            indirect.write_text(
                'PREFIX = "UPDATE "\n\n\n'
                "import sqlite3\n\n\n"
                "def touch(conn: sqlite3.Connection) -> None:\n"
                '    conn.execute(PREFIX + "draft_jobs SET status = 1")\n',
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("indirect.py", "draft_jobs")],
            "name_indirection: a census-table write assembled from a "
            "module-level name (PREFIX + 'draft_jobs SET ...') must be "
            f"recorded; the census saw {[(s.rel_path, s.table) for s in sites]} "
            "(issue #926 blind spot 4)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            whole_constant = Path(tmp) / "wholeconstant.py"
            whole_constant.write_text(
                'SQL = "DELETE FROM drafts"\n\n\n'
                "import sqlite3\n\n\n"
                "def go(conn: sqlite3.Connection) -> None:\n"
                "    conn.execute(SQL)\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("wholeconstant.py", "drafts")],
            "name_indirection: a whole-SQL module constant executed via its "
            "name counts exactly once (assignment site suppressed for "
            f"referenced names); the census saw "
            f"{[(s.rel_path, s.table) for s in sites]}",
        )
        with tempfile.TemporaryDirectory() as tmp:
            spaced_semicolon = Path(tmp) / "spacedsemicolon.py"
            spaced_semicolon.write_text(
                'Q = "SELECT 1 ;   UPDATE drafts SET title = 1"\n',
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.table) for s in sites],
            [("spacedsemicolon.py", "drafts")],
            "name_indirection: whitespace between the ';' statement anchor "
            "and the verb is tolerated (multi-statement literal); the "
            f"census saw {[(s.rel_path, s.table) for s in sites]}",
        )

    def test_census_local_shadowing_is_not_resolved(self):
        """Plan-critic round 2: a function-local assignment shadowing a
        module-level constant is NOT resolved — the fold fails toward
        missing (zero sites) rather than binding the wrong value. The
        shipped behavior is a documented limitation, pinned here so it
        cannot silently change."""
        with tempfile.TemporaryDirectory() as tmp:
            shadowed = Path(tmp) / "shadowed.py"
            shadowed.write_text(
                'PREFIX = "UPDATE "\n\n\n'
                "import sqlite3\n\n\n"
                "def touch(conn: sqlite3.Connection) -> None:\n"
                '    PREFIX = "DELETE FROM "\n'
                '    conn.execute(PREFIX + "draft_jobs SET status = 1")\n',
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            sites,
            [],
            "local_shadowing: a function-local constant shadowing the "
            "module-level name must not be resolved to either value (fail "
            "toward missing, disclosed limitation); the census reported "
            f"{sites}",
        )

    def test_census_binding_shadowing_is_not_resolved(self):
        """Feedback FB-04: every in-function binding form shadows a module
        constant — not just assignments (Copilot r4593838474). Each shape
        binds PREFIX to a non-write value locally; the fold must fail toward
        missing (zero sites) rather than resolve the module's ``UPDATE ``
        constant and report a false draft_jobs write."""
        # Every shape routes the SQL through the SHADOWED name, not a literal
        # that would be a real write regardless of name resolution.
        shapes = {
            "param": "def touch(PREFIX, conn):\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "for_target": "def touch(rows, conn):\n    for PREFIX in rows:\n        conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "with_target": "def touch(cm, conn):\n    with cm as PREFIX:\n        conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "walrus": "def touch(rows, conn):\n    if (PREFIX := rows):\n        conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "comprehension": "def touch(rows, conn):\n    return [conn.execute(PREFIX + 'draft_jobs SET status = 1') for PREFIX in rows]\n",
            "import_alias": "def touch(conn):\n    import sqlite3 as PREFIX\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "nested_def": "def touch(conn):\n    def PREFIX():\n        pass\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "tuple_unpack": "def touch(conn, rows):\n    PREFIX, other = rows\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "starred_unpack": "def touch(conn, rows):\n    first, *PREFIX = rows\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "closure_inherit": "def outer(conn):\n    PREFIX = 'DELETE FROM '\n    def inner():\n        conn.execute(PREFIX + 'draft_jobs SET status = 1')\n    return inner\n",
            "lambda_arg": "def touch(conn):\n    fn = lambda PREFIX: conn.execute(PREFIX + 'draft_jobs SET status = 1')\n    return fn\n",
            "match_capture": "def touch(conn, value):\n    match value:\n        case PREFIX:\n            conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "lambda_walrus": "def touch(conn, rows):\n    fn = lambda: conn.execute((PREFIX := rows) + 'draft_jobs SET status = 1')\n    return fn\n",
            "match_nested": "def touch(conn, value):\n    match value:\n        case SomeClass(inner=PREFIX):\n            conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "default_walrus": "def touch(conn):\n    def inner(x=(PREFIX := 'SELECT ')):\n        pass\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "class_base_walrus": "def touch(conn):\n    class C((PREFIX := object())):\n        pass\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "decorator_walrus": "def touch(conn):\n    @((PREFIX := staticmethod))\n    def inner():\n        pass\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "annotation_walrus": "def touch(conn):\n    def inner(x: (PREFIX := int)):\n        pass\n    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
            "except_binding": "def touch(conn):\n    try:\n        conn.execute('SELECT 1')\n    except Exception as PREFIX:\n        conn.execute(PREFIX + 'draft_jobs SET status = 1')\n",
        }
        for label, body in shapes.items():
            with self.subTest(shape=label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"shadow_{label}.py"
                path.write_text('PREFIX = "UPDATE "\n\n' + body, encoding="utf-8")
                sites = scan_tree(Path(tmp))
                self.assertEqual(
                    sites,
                    [],
                    f"binding_shadowing[{label}]: a local {label} binding must "
                    "shadow the module constant (fail toward missing); the "
                    f"census reported {sites}",
                )

    def test_comprehension_target_does_not_shadow_enclosing_scope(self):
        """Closeout FB-04: PEP 289 comprehension targets have their own
        scope. A comprehension that binds PREFIX must NOT suppress a later
        genuine write in the enclosing function that resolves the module
        constant of the same name."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comprehension_scope.py"
            path.write_text(
                'PREFIX = "UPDATE "\n\n'
                "def touch(conn, rows):\n"
                "    labels = [PREFIX for PREFIX in rows]\n"
                "    conn.execute(PREFIX + 'draft_jobs SET status = 1')\n"
                "    return labels\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.verb, s.table) for s in sites],
            [("comprehension_scope.py", "UPDATE", "draft_jobs")],
            "comprehension_scope: the enclosing function's PREFIX still "
            f"resolves the module constant; got {sites}",
        )

    def test_later_generator_iterable_uses_previous_targets_only(self):
        """Final FB-04: each comprehension generator's ITERABLE is evaluated
        before its own target binds, so a module constant of the same name
        there is still a real write."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "later_generator.py"
            path.write_text(
                'PREFIX = "UPDATE "\n\n'
                "def touch(conn):\n"
                "    return [1 for x in [] for PREFIX in "
                "conn.execute(PREFIX + 'draft_jobs SET status = 1')]\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.verb, s.table) for s in sites],
            [("later_generator.py", "UPDATE", "draft_jobs")],
            "later_generator_iterable: the second iterable's PREFIX resolves "
            f"the module constant; got {sites}",
        )

    def test_unreadable_directory_fails_closed(self):
        """Closeout FB-20: an inaccessible directory must abort the census,
        not silently drop a subtree that could hide a writer. Python 3.11's
        Path.rglob suppresses PermissionError; the explicit walk does not."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                Path, "iterdir", side_effect=PermissionError("access denied")
            ):
                with self.assertRaises(AssertionError) as caught:
                    scan_tree(Path(tmp))
        self.assertIn(
            "cannot read directory",
            str(caught.exception),
            "permission_failure: the census must name the unreadable "
            f"directory; got {caught.exception}",
        )

    def test_comprehension_leftmost_iterable_uses_enclosing_scope(self):
        """Closeout FB-04: PEP 289 evaluates a comprehension's LEFTMOST
        iterable in the enclosing scope, so a module constant there is still
        a real write even though the comprehension binds the same name."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comprehension_iterable.py"
            path.write_text(
                'PREFIX = "UPDATE "\n\n'
                "def touch(conn):\n"
                "    return [1 for PREFIX in "
                "conn.execute(PREFIX + 'draft_jobs SET status = 1')]\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.verb, s.table) for s in sites],
            [("comprehension_iterable.py", "UPDATE", "draft_jobs")],
            "comprehension_iterable: the leftmost iterable's PREFIX resolves "
            f"the module constant; got {sites}",
        )

    def test_nested_default_uses_enclosing_scope_despite_param_shadow(self):
        """Final FB-04: a nested function's parameter shadows the module
        constant in its BODY, but its DEFAULT expression is evaluated in the
        enclosing scope — a write there must still be counted."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested_default_scope.py"
            path.write_text(
                'PREFIX = "UPDATE "\n\n'
                "def outer(conn):\n"
                "    def inner(PREFIX=PREFIX + 'draft_jobs SET status = 1'):\n"
                "        return PREFIX\n"
                "    return inner\n",
                encoding="utf-8",
            )
            sites = scan_tree(Path(tmp))
        self.assertEqual(
            [(s.rel_path, s.verb, s.table) for s in sites],
            [("nested_default_scope.py", "UPDATE", "draft_jobs")],
            "nested_default_scope: the default's PREFIX resolves in the "
            f"enclosing scope; got {sites}",
        )

    def test_census_docstring_names_the_disclosed_limitations(self):
        """Plan-critic round 2 (round-3 fix: module docstring included): the
        #926 disclosed-limitation class (items 5-7 plus permission-denied
        traversal and local shadowing) must be NAMED in the census MODULE
        docstring — executable presence pin so the limitation lines cannot
        be silently dropped or parked in an unchecked docstring."""
        import sys

        module_doc = (sys.modules[__name__].__doc__ or "")
        class_doc = (TestTableOwnershipCensus.__doc__ or "")
        function_doc = (scan_tree.__doc__ or "")
        combined = module_doc + "\n" + class_doc + "\n" + function_doc
        self.assertIn(
            "cross-module",
            module_doc + "\n" + function_doc,
            "census_limitation_docs: the module-level census documentation "
            "must name the cross-module-constant limitation (#926 item 5)",
        )
        for limitation in (
            "job_lease",
            "symlink",
            "PermissionError",
            "shadow",
            "placeholder",  # FB-11: dynamic-in-comment/literal divergence
            "UNTERMINATED",  # FB-11: open single quote blanks the folded tail
        ):
            self.assertIn(
                limitation,
                combined,
                "census_limitation_docs: the census documentation must name "
                f"the disclosed limitation '{limitation}' (#926 items 5-7 "
                "plus round-2 additions)",
            )


if __name__ == "__main__":
    unittest.main()
