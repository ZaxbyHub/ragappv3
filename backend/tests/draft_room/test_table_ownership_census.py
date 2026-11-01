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
#: review, 8 + 7 + 11 = 26). Minimums, not equality: additive writes inside a
#: declared owner are legitimate; the pins exist so a scanner regression that
#: stops seeing writes fails loudly instead of passing vacuously.
_KNOWN_MINIMUMS = {
    "api/routes/draft_room.py": 8,
    "services/draft_job_processor.py": 7,
    "services/draft_pipeline.py": 11,
}

#: ``DraftStore`` is the DEFAULT owner of these tables (its module docstring
#: says so), so its own write surface is pinned too. Without this the store
#: could be gutted of every six-table write and the whole census would stay
#: green — the outside-store total excludes the store by construction. 48 sites
#: at base 3e00fbf6; the margin absorbs legitimate refactors.
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


def _fold(node: ast.AST) -> str | None:
    """Best-effort static value of a string-bearing expression.

    Dynamic parts collapse to ``_DYNAMIC`` so a verb followed by a dynamic
    table reference cannot match a real table name (no false positive), while
    a verb and table both inside the static prefix still match (no false
    negative for folded/concatenated/f-string SQL).
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
        left = _fold(node.left)
        right = _fold(node.right)
        if left is None or right is None:
            return None
        return left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _fold(node.func.value)
    return None


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Identity of every docstring node in ``tree`` (module, class, function).

    Docstrings are documentation, not SQL; without this exclusion a docstring
    describing an UPDATE would count as a write.
    """
    ids: set[int] = set()

    def first_stmt_doc(body: list[ast.stmt]) -> None:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))

    first_stmt_doc(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first_stmt_doc(node.body)
    return ids


def scan_tree(root: Path) -> list[WriteSite]:
    """Census ``root`` for write expressions against the six tables.

    Fails closed: a file that cannot be decoded or parsed raises
    ``AssertionError`` naming it, because an unreadable file could hide
    writers. Decoding is ``utf-8-sig`` so a Windows-authored BOM (which
    CPython imports happily) is not misreported as a syntax error.
    """
    sites: list[WriteSite] = []
    for path in sorted(root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"census cannot decode {path}: {exc}") from exc
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise AssertionError(f"census cannot parse {path}: {exc}") from exc
        skip = _docstring_ids(tree)

        def visit(node: ast.AST) -> None:
            if id(node) in skip:
                return
            folded = None
            if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
                folded = _fold(node)
            if folded is not None:
                matched = False
                for match in _VERB_RE.finditer(folded):
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
            for child in ast.iter_child_nodes(node):
                visit(child)

        visit(tree)
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
        for module, minimum in _KNOWN_MINIMUMS.items():
            self.assertGreaterEqual(
                by_module.get(module, 0),
                minimum,
                f"{module} carried {minimum} declared write sites at issue "
                f"#790's base but the census now sees "
                f"{by_module.get(module, 0)}",
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


if __name__ == "__main__":
    unittest.main()
