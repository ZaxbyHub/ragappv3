"""Contract test: the documented vault permission matrix cannot drift from
the real policy code (issue #560, check C6).

Doc parse contract (EXACT duplicate of test_vault_matrix_doc.py's contract
— the small parser is deliberately duplicated, not imported):

docs/admin-guide.md must contain a section whose heading contains both
"Vault permission" and "matrix" (case-insensitive); within it a markdown
table whose header row includes both the words "Operation" and "Minimum
vault permission" (case-insensitive), with at least 8 data rows, every
row's level cell being exactly one of read/write/admin. The operation
column is the header cell containing "operation"; the level column is the
header cell containing "permission"; cells are split on "|" with
backticks/whitespace stripped.

Census pinning: the matrix data rows are pinned, row by row, to the code
census in .agents/issue-traces/560-permission-model-explicit/04-root-cause.md
section C25. A documented row must match at least one pinned operation
(keyword set below), every pinned operation must be documented at least
once, and every matched pin must agree with the documented level cell:

    read : list documents / search documents / view documents /
           fetch a single document / raw artifact /
           view chat sessions / list chat sessions
    write: upload documents / create chat session / send chat messages /
           wiki / kms / folder / memory mutations (create, edit, update,
           delete, rename, move, add, import)
    admin: delete documents / manage vault members (manage, add, remove,
           update, delete, assign) / grant or revoke group access /
           update, delete, rename, or reconfigure a vault

Code verification: each level name is checked against the real
VAULT_PERMISSION_LEVELS / VAULT_ACTION_LEVELS ordering via behavioral
evaluate_policy() calls on a temp sqlite DB — the full pass/fail grid for
read/write/admin members over the read/write/delete/admin actions, plus a
boundary check per documented level (a member granted exactly the level
passes its representative action; the next-weaker principal fails it).
A documented cell that contradicts code fails this test.

Base-expected outcome: RED (DISCRIMINATING) — the doc section does not
exist at base, so the two doc-facing tests fail; the pure code-grid test
passes at base (the evaluator already behaves this way — the defect is
that nothing documents or pins it).
"""

import re
import sqlite3
from pathlib import Path

from app.services.authz_policy import (
    VAULT_ACTION_LEVELS,
    VAULT_PERMISSION_LEVELS,
    evaluate_policy,
)

ADMIN_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "admin-guide.md"

VALID_LEVELS = ("read", "write", "admin")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SEPARATOR = re.compile(r"^[\s:\-|]+$")

# Mutation verbs that turn a domain keyword (wiki/kms/folder/memory) row
# into a content-mutation row (write level per the code census).
VERB_MUTATE = (
    "creat",
    "updat",
    "delet",
    "edit",
    "renam",
    "mov",
    "mutat",
    "writ",
    "add",
    "import",
)
VERB_MEMBER = ("manage", "add", "remove", "updat", "delet", "assign")
VERB_GROUP = ("grant", "revoke", "manage", "access", "assign")
VERB_VAULT_ADMIN = ("updat", "delet", "renam", "setting")

# (required keyword substrings — all must appear in the operation cell,
#  case-insensitive; optional any-of verb stems — at least one must appear;
#  the census-required level). This pin list is exhaustive: every matrix
# data row must match one of these entries, and every entry must be
# covered by at least one row.
PINNED_OPERATIONS: tuple[tuple[tuple[str, ...], tuple[str, ...] | None, str], ...] = (
    (("list", "document"), None, "read"),
    (("search", "document"), None, "read"),
    (("view", "document"), None, "read"),
    (("fetch", "document"), None, "read"),
    (("artifact",), None, "read"),
    (("view", "session"), None, "read"),
    (("list", "session"), None, "read"),
    (("upload",), None, "write"),
    (("create", "session"), None, "write"),
    (("send", "message"), None, "write"),
    (("wiki",), VERB_MUTATE, "write"),
    (("kms",), VERB_MUTATE, "write"),
    (("folder",), VERB_MUTATE, "write"),
    (("memor",), VERB_MUTATE, "write"),
    (("delet", "document"), None, "admin"),
    (("member",), VERB_MEMBER, "admin"),
    (("group",), VERB_GROUP, "admin"),
    (("vault",), VERB_VAULT_ADMIN, "admin"),
)


# ----------------------------------------------------------------------
# Doc parser (duplicated from test_vault_matrix_doc.py by design)
# ----------------------------------------------------------------------


def _find_matrix_section(text: str) -> list[str]:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        match = _HEADING.match(line)
        if not match:
            continue
        title = match.group(2).lower()
        if "vault permission" in title and "matrix" in title:
            level = len(match.group(1))
            body: list[str] = []
            for follow in lines[idx + 1 :]:
                next_heading = _HEADING.match(follow)
                if next_heading and len(next_heading.group(1)) <= level:
                    break
                body.append(follow)
            return body
    raise AssertionError(
        "docs/admin-guide.md has no section heading containing both "
        "'Vault permission' and 'matrix'"
    )


def _cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip().strip("`").strip() for cell in stripped.split("|")]


def _parse_matrix_rows(section_lines: list[str]) -> list[tuple[str, str]]:
    """Return [(operation_text, level), ...] from the section's table."""
    table_lines: list[str] = []
    for line in section_lines:
        if line.strip().startswith("|"):
            table_lines.append(line.strip())
        elif table_lines:
            break
    if len(table_lines) < 3:
        raise AssertionError(
            "the Vault permission matrix section has no markdown table "
            "with a header row, a separator row, and data rows"
        )

    header = [cell.lower() for cell in _cells(table_lines[0])]
    if not _SEPARATOR.match(table_lines[1]):
        raise AssertionError(
            f"matrix table separator row is malformed: {table_lines[1]!r}"
        )
    op_col = next(
        (i for i, cell in enumerate(header) if "operation" in cell), None
    )
    level_col = next(
        (i for i, cell in enumerate(header) if "permission" in cell), None
    )
    if op_col is None or level_col is None or op_col == level_col:
        raise AssertionError(
            "matrix table header must contain an 'Operation' column and a "
            f"'Minimum vault permission' column, got: {header!r}"
        )
    joined = " ".join(header)
    assert "minimum vault permission" in joined, (
        f"matrix header must include 'Minimum vault permission': {header!r}"
    )

    rows: list[tuple[str, str]] = []
    for line in table_lines[2:]:
        cells = _cells(line)
        rows.append((cells[op_col], cells[level_col].lower()))
    return rows


def _doc_rows() -> list[tuple[str, str]]:
    """Parsed (operation, level) rows from the documented matrix."""
    text = ADMIN_GUIDE.read_text(encoding="utf-8")
    rows = _parse_matrix_rows(_find_matrix_section(text))
    assert len(rows) >= 8, f"matrix must have at least 8 data rows, got {len(rows)}"
    for op_text, level in rows:
        assert level in VALID_LEVELS, (
            f"level cell for {op_text!r} must be read/write/admin, got {level!r}"
        )
    return rows


# ----------------------------------------------------------------------
# Code-side harness: temp DB sufficient for get_effective_vault_permissions
# ----------------------------------------------------------------------

_POLICY_SCHEMA = """
CREATE TABLE vaults (
    id INTEGER PRIMARY KEY, name TEXT, description TEXT,
    org_id INTEGER, visibility TEXT, owner_id INTEGER
);
CREATE TABLE vault_members (
    vault_id INTEGER NOT NULL, user_id INTEGER NOT NULL, permission TEXT NOT NULL
);
CREATE TABLE vault_group_access (
    vault_id INTEGER NOT NULL, group_id INTEGER NOT NULL, permission TEXT NOT NULL
);
CREATE TABLE group_members (
    group_id INTEGER NOT NULL, user_id INTEGER NOT NULL
);
CREATE TABLE org_members (
    org_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL
);
"""


def _policy_db() -> sqlite3.Connection:
    """In-memory DB with one private vault (id 1) and members granted
    exactly read (user 101), write (102), admin (103); user 104 is not a
    member. check_same_thread=False because evaluate_policy runs its
    queries via asyncio.to_thread."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript(_POLICY_SCHEMA)
    db.execute(
        "INSERT INTO vaults (id, name, description, org_id, visibility, owner_id) "
        "VALUES (1, 'V', 'd', NULL, 'private', 103)"
    )
    db.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 101, 'read')"
    )
    db.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 102, 'write')"
    )
    db.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (1, 103, 'admin')"
    )
    db.commit()
    return db


def _principal(user_id: int) -> dict:
    return {"id": user_id, "username": f"user{user_id}", "role": "member"}


def _row_matches(op_text: str, keywords: tuple[str, ...], verbs: tuple[str, ...] | None) -> bool:
    lowered = op_text.lower()
    if not all(keyword in lowered for keyword in keywords):
        return False
    if verbs is not None and not any(verb in lowered for verb in verbs):
        return False
    return True


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestVaultMatrixContract:
    """Doc rows vs the hard-coded code census and vs real evaluate_policy."""

    def test_documented_rows_match_code_census(self):
        """Every documented (operation, level) cell equals the level the
        code census requires; every census operation is documented."""
        rows = _doc_rows()

        for op_text, level in rows:
            matched = {
                expected
                for keywords, verbs, expected in PINNED_OPERATIONS
                if _row_matches(op_text, keywords, verbs)
            }
            assert matched, (
                f"documented matrix row {op_text!r} matches no pinned "
                "census operation — either the row is not one of the "
                "census operations (remove it) or the census pin in this "
                "test must be extended in lockstep with a code change"
            )
            assert matched == {level}, (
                f"documented level {level!r} for {op_text!r} contradicts "
                f"the code census requirement {sorted(matched)}"
            )

        uncovered = [
            (keywords, verbs, expected)
            for keywords, verbs, expected in PINNED_OPERATIONS
            if not any(
                _row_matches(op_text, keywords, verbs) for op_text, _ in rows
            )
        ]
        assert not uncovered, (
            "census operations missing from the documented matrix: "
            f"{uncovered}"
        )

    async def test_documented_levels_match_real_policy_boundaries(self):
        """For every distinct documented level L, a real member granted
        exactly L passes a representative action while the next-weaker
        principal fails it — the doc's level names map onto real
        evaluate() gates."""
        db = _policy_db()
        documented_levels = {level for _, level in _doc_rows()}

        # level -> (representative action, weaker principal, boundary
        # principal). User 104 has no membership at all (below read).
        boundaries = {
            "read": ("read", 104, 101),
            "write": ("write", 101, 102),
            "admin": ("delete", 102, 103),
        }
        for level in sorted(documented_levels):
            action, weaker, boundary = boundaries[level]
            weaker_result = await evaluate_policy(
                db, _principal(weaker), "vault", 1, action
            )
            assert weaker_result is False, (
                f"principal weaker than documented level {level!r} must "
                f"fail action {action!r}"
            )
            boundary_result = await evaluate_policy(
                db, _principal(boundary), "vault", 1, action
            )
            assert boundary_result is True, (
                f"principal granted exactly documented level {level!r} "
                f"must pass action {action!r}"
            )
        db.close()

    async def test_evaluate_policy_grid_matches_level_maps(self):
        """Full behavioral grid over the real evaluator: for every
        granted permission and every action,
        evaluate == (VAULT_PERMISSION_LEVELS[p] >= VAULT_ACTION_LEVELS[a]).
        Passes at base — it proves the code side of the contract the doc
        must describe."""
        db = _policy_db()
        for user_id, granted in ((101, "read"), (102, "write"), (103, "admin")):
            for action in VAULT_ACTION_LEVELS:
                result = await evaluate_policy(
                    db, _principal(user_id), "vault", 1, action
                )
                expected = (
                    VAULT_PERMISSION_LEVELS[granted]
                    >= VAULT_ACTION_LEVELS[action]
                )
                assert result is expected, (
                    f"member granted {granted!r} action {action!r}: "
                    f"evaluate={result}, ordering says {expected}"
                )
        # A non-member fails every action.
        for action in VAULT_ACTION_LEVELS:
            result = await evaluate_policy(db, _principal(104), "vault", 1, action)
            assert result is False
        db.close()
