"""K7 guardrail — census of FTS5 snippet()/highlight() call sites (AC7).

Trace: kms-fts-snippet-large-body-wedge. The wedge class is "FTS5 auxiliary
function evaluated over an unbounded column". This guardrail scans every
non-test .py file under backend/app for textual snippet(/highlight( call
sites and requires each site to be either:

(a) wrapped by ITS OWN NUL-proof length guard — a ``CASE WHEN length(CAST(
    <col> AS BLOB))`` where ``<col>`` is the column the call targets
    (table+index resolved through _TABLE_COLUMNS), appearing BEFORE the call
    in the same string literal with no other snippet(/highlight( occurrence
    between that guard and this site (so a sibling guard for a different
    call/column never exempts this one), or
(b) on a documented bounded-metadata column — the call directly names the
    files_search_fts metadata table within 40 chars after the call (the
    documents.py _meta_snippet helper shape).

Review F-001 drove the byte-length requirement: plain ``length()`` stops at
the first NUL byte, so a NUL-prefixed body measured a tiny length and the
guard admitted snippet() over the full column (probed 334–504 s). Only the
CAST-AS-BLOB shape counts as guarded. Review F-002/RC3 drove the association,
column-binding, regex, predecessor and path-exclusion hardening below; the
sensitivity suite now plants every previously-failing form.

Scanner mechanics: only STRING tokens in expression position are scanned (the
previous significant token — skipping NL/COMMENT/INDENT/DEDENT/ENCODING, but
NOT NEWLINE, which marks statement position, i.e. a docstring — is an
expression operator, the 'return' keyword, or a preceding STRING of an
implicitly-concatenated literal run). Statement-position strings (docstrings)
and # comments therefore never produce sites, and the identifier lookbehind
(?<![A-Za-z0-9_]) keeps Python helper names out. The site regex is
case-insensitive and tolerates whitespace before the paren, so SQLite-valid
``SNIPPET( `` / ``snippet (`` shapes are found too.
"""

from __future__ import annotations

import io
import re
import tokenize
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# Case-insensitive + whitespace-tolerant (RC3c: SNIPPET( / "snippet (" are
# valid SQLite and were previously invisible).
_SITE_RE = re.compile(r"(?<![A-Za-z0-9_])(snippet|highlight)\s*\(", re.IGNORECASE)

# NUL-proof guard shape ONLY (F-001): captures the guarded column reference,
# e.g. "e.body" or "f.parsed_text".
_BLOB_GUARD_RE = re.compile(
    r"length\s*\(\s*cast\s*\(\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+as\s+blob\s*\)",
    re.IGNORECASE,
)

_BOUNDED_META_TABLE = "files_search_fts"

# Issue #1005: the files_search_fts columns that are bounded at
# write/validation time and therefore exempt from the length guard. The two
# email columns are stored uncapped from mail headers and are deliberately
# NOT in this allowlist — their snippets must carry the
# length(CAST(col AS BLOB)) guard. A future metadata column is not exempt
# until it is added here WITH a bounded-writer justification.
_BOUNDED_META_COLUMNS = {
    "file_name",
    "file_type",
    "status",
    "source",
    "document_date",
}

# Sentinel: the call's column index is an f-string {placeholder} token —
# the recognized dynamic-helper shape whose guards ship as literals.
_DYNAMIC_TARGET = object()

_META_LOOKAHEAD_CHARS = 40

# FTS5 column order per table — used to bind a guard to the column the
# snippet() call actually reads (RC3-association: a guard on a different
# column must not exempt this site).
_TABLE_COLUMNS = {
    "kms_entries_fts": ["title", "summary", "body", "tags_json"],
    "files_content_fts": ["parsed_text"],
    "files_search_fts": [
        "file_name",
        "file_type",
        "status",
        "source",
        "email_subject",
        "email_sender",
        "document_date",
    ],
}

# Expression-context predecessors: a STRING token is scannable SQL/expression
# text iff the previous significant token (skipping NL, COMMENT, INDENT,
# DEDENT, ENCODING — but NOT NEWLINE, which terminates a logical line and
# therefore marks the next string as statement position, i.e. a docstring)
# is one of these operators, the 'return' keyword, or a STRING (implicit
# concatenation of adjacent literals — RC3d: `sql += "snippet(…)"` tokenizes
# as OP '+=' and `"a" "b"` makes the second piece's predecessor STRING; both
# were blind spots that let unguarded sites pass).
_EXPR_PREDECESSOR_OPS = {
    "=",
    "==",
    "(",
    ",",
    "%",
    "+",
    "+=",
    "*",
    "{",
    "[",
    ":",
    "and",
    "or",
}
_EXPR_PREDECESSOR_NAMES = {"return"}

_TOKEN_SKIP_TYPES = {
    tokenize.NL,
    tokenize.COMMENT,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
}


@dataclass
class SnippetSite:
    """One textual FTS5-auxiliary call site found in app source."""

    rel_path: str
    line: int
    kind: str  # "snippet" | "highlight"
    verdict: str  # "guarded" | "bounded-metadata" | "unguarded"

    def __str__(self) -> str:  # pragma: no cover — diagnostic formatting
        return f"{self.rel_path}:{self.line} {self.kind}( -> {self.verdict}"


def _target_column(token_text: str, match: re.Match):
    """Resolve the column a snippet()/highlight() call reads.

    Returns the column name, the sentinel ``_DYNAMIC_TARGET`` when the
    call's index position is an f-string ``{placeholder}`` token (the
    recognized dynamic-helper shape — the live metadata arm builds its
    calls through ``_meta_snippet(col)``), or ``None`` when the target is
    invalid (unknown table, out-of-range or unparseable literal index).
    ``None`` is treated by the classifier as UNGUARDED — a site the scanner
    cannot bind must fail loud, not silent. An invalid literal index (e.g.
    column 99) is NOT exempt: only the dynamic helper keeps the
    bounded-metadata compatibility exemption (issue #1005).
    """
    after = token_text[match.end() :]
    m = re.match(r"\s*([A-Za-z_]\w*)\s*,\s*(\{[^}]*\}|\d+)", after)
    if not m:
        return None
    if not m.group(2).isdigit():
        return _DYNAMIC_TARGET
    columns = _TABLE_COLUMNS.get(m.group(1))
    if columns is None:
        return None
    idx = int(m.group(2))
    if idx >= len(columns):
        return None
    return columns[idx]


def _classify_site(token_text: str, match: re.Match) -> str:
    """Classify one snippet(/highlight( occurrence inside a string literal."""
    start = match.start()
    tail = token_text[start : start + _META_LOOKAHEAD_CHARS]
    target = _target_column(token_text, match)
    if _BOUNDED_META_TABLE in tail and (
        target is _DYNAMIC_TARGET or target in _BOUNDED_META_COLUMNS
    ):
        # Bounded metadata: either the column is write-bounded by contract
        # (the allowlist below — NOT the uncapped email columns, issue
        # #1005), or the site is the recognized dynamic helper whose column
        # index is an f-string placeholder (its guards ship as literals in
        # the same module and are pinned by the k7c/k7e suites).
        return "bounded-metadata"
    if target is None or target is _DYNAMIC_TARGET:
        return "unguarded"
    # Nearest preceding NUL-proof guard whose column matches THIS call and
    # which has no other snippet(/highlight( between it and this site
    # (F-002/RC3a: a sibling guard for another call/column must never exempt
    # this one — deleting this site's own guard while a neighbor guard stays
    # in range must flip the verdict to unguarded).
    for guard in _BLOB_GUARD_RE.finditer(token_text, 0, start):
        guarded_col = guard.group(1).split(".")[-1].lower()
        if guarded_col != target:
            continue
        if _SITE_RE.search(token_text, guard.end(), start):
            continue  # an aux call sits between this guard and ours
        return "guarded"
    return "unguarded"


def _scannable_string_tokens(source: str):
    """Yield STRING tokens that sit in expression (non-docstring) position."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    prev = None  # (type, string) of the last non-skipped significant token
    for tok in tokens:
        if tok.type in _TOKEN_SKIP_TYPES:
            continue
        if tok.type == tokenize.STRING:
            if prev is not None and (
                (prev[0] == tokenize.OP and prev[1] in _EXPR_PREDECESSOR_OPS)
                or (prev[0] == tokenize.NAME and prev[1] in _EXPR_PREDECESSOR_NAMES)
                or prev[0] == tokenize.STRING
            ):
                yield tok
            # A statement-position string (docstring) is not scanned, but it
            # IS the previous significant token for whatever follows.
            prev = (tok.type, tok.string)
            continue
        prev = (tok.type, tok.string)


def _is_excluded(app_root: Path, path: Path) -> bool:
    """True for test paths only — never for production modules (RC3c).

    The old substring test skipped ANY path component containing "test", which
    silently excluded the production module ab_testing.py (and would exclude
    whole trees under checkout dirs like latest/). Exclusion is now: a
    DIRECTORY component of the app-root-relative path equal to tests/test, or
    a FILENAME matching test_*.py / *_test.py.
    """
    rel = path.relative_to(app_root)
    parts = rel.parts
    if any(part.lower() in ("tests", "test") for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    return name.startswith("test_") or name.endswith("_test.py")


def classify_source(rel_path: str, source: str) -> List[SnippetSite]:
    """Return every FTS5-auxiliary call site in one source file's text."""
    sites: List[SnippetSite] = []
    for tok in _scannable_string_tokens(source):
        for match in _SITE_RE.finditer(tok.string):
            line = tok.start[0] + tok.string.count("\n", 0, match.start())
            sites.append(
                SnippetSite(
                    rel_path=rel_path,
                    line=line,
                    kind=match.group(1).lower(),
                    verdict=_classify_site(tok.string, match),
                )
            )
    return sites


def scan_app_tree(app_root: Path = _APP_ROOT) -> List[SnippetSite]:
    """Scan every non-test .py file under app_root for call sites."""
    sites: List[SnippetSite] = []
    unscannable: List[str] = []
    for path in sorted(app_root.rglob("*.py")):
        if _is_excluded(app_root, path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unscannable.append(f"{path} (read: {exc})")
            continue
        try:
            file_sites = classify_source(str(path), source)
        except tokenize.TokenError as exc:
            unscannable.append(f"{path} (tokenize: {exc})")
            continue
        sites.extend(file_sites)
    if unscannable:
        raise AssertionError(
            "census could not scan some backend/app files: " + "; ".join(unscannable)
        )
    return sites


class TestK7AC7SnippetGuardCensus(unittest.TestCase):
    """AC7 (NEW-SURFACE guardrail): no unguarded FTS5 auxiliary call sites."""

    def test_k7_ac7_census_no_unguarded_fts5_snippet_or_highlight_sites(self):
        sites = scan_app_tree()

        # Scanner-sensitivity floor: the app tree is known to carry several
        # snippet( sites today; a scanner that finds nothing must not pass.
        self.assertGreaterEqual(
            len(sites),
            4,
            f"census found only {len(sites)} snippet/highlight sites under "
            f"backend/app — the scanner must find the known surface; got "
            f"{[str(s) for s in sites]}",
        )

        unguarded = [s for s in sites if s.verdict == "unguarded"]
        self.assertEqual(
            [],
            unguarded,
            "unguarded FTS5 snippet()/highlight() call sites under "
            "backend/app (each must carry its own NUL-proof "
            "CASE WHEN length(CAST(<col> AS BLOB)) guard for the column it "
            "reads, in the same SQL literal before the call, or be a "
            "snippet(files_search_fts, ...) bounded-metadata call): "
            + "; ".join(str(s) for s in unguarded),
        )


class TestK7AC7ScannerSensitivity(unittest.TestCase):
    """Pins the classifier so the census cannot rot into a no-op."""

    def test_k7_ac7_scanner_classifies_planted_guarded_and_metadata_sites(self):
        # A planted UNguarded site shaped like wiki_store.py source: a bare
        # snippet( over a body-ish FTS column in a returned SQL literal.
        planted_wiki = (
            "def _search(self, conn):\n"
            "    return (\n"
            "        \"SELECT id, snippet(wiki_entries_fts, 2, '<mark>', "
            "'</mark>', '…', 24) AS body_snippet FROM wiki_entries_fts "
            "WHERE wiki_entries_fts MATCH ?\"\n"
            "    )\n"
        )
        planted = classify_source(
            "backend/app/services/wiki_store.py", planted_wiki
        )
        self.assertEqual(
            ["unguarded"],
            [s.verdict for s in planted],
            f"planted unguarded snippet( must be flagged: {planted}",
        )

        # A planted UNguarded highlight( must be flagged too.
        planted_highlight = (
            "def _h(self, conn):\n"
            "    sql = (\n"
            "        \"SELECT highlight(wiki_entries_fts, 2, '[', ']') "
            "AS body_hi FROM wiki_entries_fts\"\n"
            "    )\n"
            "    return sql\n"
        )
        planted_hl = classify_source(
            "backend/app/services/wiki_store.py", planted_highlight
        )
        self.assertEqual(
            ["unguarded"],
            [s.verdict for s in planted_hl],
            f"planted unguarded highlight( must be flagged: {planted_hl}",
        )

        # Guard shape (a): the NUL-proof byte-length guard bound to the SAME
        # column the call reads (F-001 + F-002 column-binding).
        guarded_src = (
            "def _g(self, conn):\n"
            "    return (\n"
            "        \"SELECT id, CASE WHEN length(CAST(e.body AS BLOB)) <= "
            "131072 THEN "
            "snippet(kms_entries_fts, 2, '<mark>', '</mark>', '…', 24) "
            "ELSE substr(e.body, 1, 4000) END AS body_snippet "
            "FROM kms_entries_fts WHERE kms_entries_fts MATCH ?\"\n"
            "    )\n"
        )
        guarded = classify_source(
            "backend/app/services/kms_retrieval.py", guarded_src
        )
        self.assertEqual(
            ["guarded"],
            [s.verdict for s in guarded],
            f"NUL-proof CAST-AS-BLOB guard must be guarded: {guarded}",
        )

        # Guard shape (b): the files_search_fts bounded-metadata call.
        meta_src = (
            "def _meta_snippet(col):\n"
            "    return (\n"
            "        f\"snippet(files_search_fts, {col}, '<mark>', "
            "'</mark>', '…', 24)\"\n"
            "    )\n"
        )
        meta = classify_source(
            "backend/app/api/routes/documents.py", meta_src
        )
        self.assertEqual(
            ["bounded-metadata"],
            [s.verdict for s in meta],
            f"files_search_fts metadata snippet( must be exempt: {meta}",
        )

        # Non-sites: comments, docstrings, and Python identifiers.
        noise_src = (
            "# call snippet() over the body column\n"
            "def _f():\n"
            '    """Docs mention snippet() and _meta_snippet( calls."""\n'
            "    x = _unified_snippet(a, b)\n"
            "    y = _meta_snippet(4)\n"
            "    return x + y\n"
        )
        noise = classify_source("backend/app/api/routes/search.py", noise_src)
        self.assertEqual(
            [],
            noise,
            f"comments/docstrings/identifier names must not be sites: {noise}",
        )

    def test_k7_ac7_scanner_flags_previously_invisible_forms(self):
        # RC3a / F-002 (a): sibling-inheritance. A guard for a DIFFERENT
        # call earlier in the same literal must not exempt a later unguarded
        # call — deleting one call's own guard while its neighbor's stays
        # in range must flip it to unguarded.
        sibling = (
            "def _g(self, conn):\n"
            "    return (\n"
            "        \"SELECT "
            "CASE WHEN length(CAST(e.summary AS BLOB)) <= 131072 "
            "THEN snippet(kms_entries_fts, 1, 'x', 'y', '…', 24) END AS s, "
            "snippet(kms_entries_fts, 2, 'x', 'y', '…', 24) AS b "
            "FROM kms_entries_fts\"\n"
            "    )\n"
        )
        sibling_sites = classify_source("backend/app/services/kms_retrieval.py", sibling)
        self.assertEqual(
            ["guarded", "unguarded"],
            [s.verdict for s in sibling_sites],
            f"sibling guard must not exempt the later call: {sibling_sites}",
        )

        # RC3-association: a guard on a MISMATCHED column (summary) must not
        # exempt a body call even with nothing between them.
        mismatch = (
            "def _g(self, conn):\n"
            "    return (\n"
            "        \"SELECT CASE WHEN length(CAST(e.summary AS BLOB)) <= 1 "
            "THEN snippet(kms_entries_fts, 2, 'x', 'y', '…', 24) END "
            "FROM kms_entries_fts\"\n"
            "    )\n"
        )
        mismatch_sites = classify_source("backend/app/services/kms_retrieval.py", mismatch)
        self.assertEqual(
            ["unguarded"],
            [s.verdict for s in mismatch_sites],
            f"mismatched-column guard must not exempt the call: {mismatch_sites}",
        )

        # F-001: the plain character-length guard (bypassable by an embedded
        # NUL) must NOT count as guarded — only the CAST-AS-BLOB shape does.
        char_guard = (
            "def _g(self, conn):\n"
            "    return (\n"
            "        \"SELECT CASE WHEN length(e.body) <= 131072 "
            "THEN snippet(kms_entries_fts, 2, 'x', 'y', '…', 24) END "
            "FROM kms_entries_fts\"\n"
            "    )\n"
        )
        char_sites = classify_source("backend/app/services/kms_retrieval.py", char_guard)
        self.assertEqual(
            ["unguarded"],
            [s.verdict for s in char_sites],
            f"char-only length guard is bypassable and must not count: {char_sites}",
        )

        # RC3b: `sql += "…"` (predecessor OP '+=') must be scanned.
        plus_eq = (
            "sql = 'SELECT 1'\n"
            "sql += \"SELECT snippet(kms_entries_fts, 2, 'x', 'y', '…', 24) "
            "FROM kms_entries_fts\"\n"
        )
        plus_eq_sites = classify_source("backend/app/services/kms_store.py", plus_eq)
        self.assertEqual(
            ["unguarded"],
            [s.verdict for s in plus_eq_sites],
            f"sql += constructed sites must be found: {plus_eq_sites}",
        )

        # RC3b: implicitly-concatenated adjacent literals (second piece's
        # predecessor is a STRING) must be scanned.
        adjacent = (
            "sql = ('SELECT snippet(kms_entries_fts, 2, 1) '\n"
            "       'FROM kms_entries_fts')\n"
        )
        adjacent_sites = classify_source("backend/app/services/kms_store.py", adjacent)
        self.assertEqual(
            ["unguarded"],
            [s.verdict for s in adjacent_sites],
            f"adjacent-literal continuation sites must be found: {adjacent_sites}",
        )

        # RC3c: uppercase and whitespace-tolerant variants are valid SQLite.
        variants = (
            "def _g(self, conn):\n"
            "    return (\n"
            "        \"SELECT SNIPPET(kms_entries_fts, 2, 'x', 'y', '…', 24) "
            "FROM kms_entries_fts\"\n"
            "    )\n"
        )
        variant_sites = classify_source("backend/app/services/kms_retrieval.py", variants)
        self.assertEqual(
            ["unguarded"],
            [s.verdict for s in variant_sites],
            f"case/whitespace variants must be found: {variant_sites}",
        )

    def test_k7_ac7_path_exclusion_spares_production_module_names(self):
        # RC3c: production files whose name merely CONTAINS "test" (the old
        # substring rule's ab_testing.py casualty) must be scanned, while
        # real test paths stay excluded.
        root = Path("/repo/backend/app")
        cases = [
            (root / "services" / "ab_testing.py", False),  # was silently excluded
            (root / "services" / "latest_sync.py", False),  # "latest" contains "test"
            (root / "services" / "kms_store.py", False),
            (root / "tests" / "helper.py", True),  # tests/ dir
            (root / "test_helpers.py", True),  # test_ prefix
            (root / "services" / "legacy_test.py", True),  # _test.py suffix
        ]
        for path, expected in cases:
            self.assertEqual(
                expected,
                _is_excluded(root, path),
                f"exclusion mismatch for {path}",
            )


if __name__ == "__main__":
    unittest.main()
