"""Status-transition map guardrail for issue #927 (extends the #790 census).

The table-ownership census (``test_table_ownership_census.py``) counts verb+table
pairs and floors them — correct for ownership, blind for transition discipline.
Issue #790's Constraints promise the census "enforces the existing
``_check_transition`` discipline is not further eroded"; a new ad-hoc
``UPDATE drafts SET status = '<anything>'`` inside a declared co-owner raises
that co-owner's count above its floor and stays green. This guard closes that
gap: every status write in the three declared co-owner modules
(``api/routes/draft_room.py``, ``services/draft_job_processor.py``,
``services/draft_pipeline.py``) must resolve to a registered transition source
— a DraftStore transition call, a named mirrored prior-status allowlist, a
located inline guard, or the documented settle-pair chain — and every prior ->
target pair must be a legal edge of the store's per-table transition maps
(``_DRAFT/_FINDING/_JOB/_INPUT_TRANSITIONS``), a same-target write, or a
registered composite path. Unresolved writes — including parameterized writes
without a located literal constraint — fail closed, naming file:line and the
literal/status found. RED at base (the file did not exist), GREEN after the
guard ships with this PR.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_store import (  # noqa: E402
    _DRAFT_TRANSITIONS,
    _FINDING_TRANSITIONS,
    _INPUT_TRANSITIONS,
    _JOB_TRANSITIONS,
)

_BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

#: The three declared co-owner modules (the #790 owner map).
CO_OWNER_MODULES = (
    "api/routes/draft_room.py",
    "services/draft_job_processor.py",
    "services/draft_pipeline.py",
)

#: Transition map per census table (table-specific resolution: a literal is
#: validated only against the map of the table it is written to).
TRANSITION_MAPS = {
    "drafts": _DRAFT_TRANSITIONS,
    "draft_findings": _FINDING_TRANSITIONS,
    "draft_jobs": _JOB_TRANSITIONS,
    "draft_inputs": _INPUT_TRANSITIONS,
}

#: Mirrored allowlists this guard pins to the store's tables (constant name ->
#: (module, registered value)). The test asserts each constant exists in its
#: module with EXACTLY this value, so the mirrored sets cannot drift from the
#: registry (and the registry cannot invent them).
MIRRORED_ALLOWLISTS = {
    "_COMPILE_ALLOWED_PRIOR_STATUSES": (
        "api/routes/draft_room.py",
        frozenset({"draft", "needs_review", "failed", "cancelled", "ready"}),
    ),
    "_APPLY_FINDING_ALLOWED_PRIOR_STATUSES": (
        "api/routes/draft_room.py",
        frozenset({"draft", "needs_review", "ready", "failed", "cancelled"}),
    ),
    "_ASSEMBLE_ALLOWED_PRIOR_DRAFT_STATUSES": (
        "services/draft_pipeline.py",
        frozenset({"queued", "running", "needs_review"}),
    ),
}

# Entry kinds:
#   "allowlist"    — priors are the named mirrored constant; every prior must
#                    be a legal prior (direct edge, same-target, or composite).
#   "inline"       — priors are a located inline predicate; the registered
#                    predicate text must appear in the function source.
#   "chain"        — the settle-pair chain: prior states are established by
#                    the store claim (job pending->running) and
#                    store.set_input_parse_status(target="parsing"); the test
#                    asserts those establishers exist in the codebase.
# composite_priors lists priors that are NOT a direct edge / same-target and
# names the structural justification the test asserts:
#   "same_function_predecessor:<fn>:<literal>" — another registered write in
#       the same function moves the draft onto <literal>'s edge first.
#   "store_claim_moves_prior:<prior>:<via>" — a DraftStore claim performs
#       prior -> via before this write's edge applies.


@dataclass
class RegistryEntry:
    priors: frozenset
    kind: str
    guard_text: str = ""
    composite: dict = field(default_factory=dict)
    #: For parameterized writes (status == "?"): the located literal constraint
    #: on the bound parameter. Every (prior, target) pair must be a legal edge.
    targets: frozenset | None = None


REGISTRY = {
    # ── api/routes/draft_room.py ─────────────────────────────────────────
    ("api/routes/draft_room.py", "_sync_enqueue_compile", "drafts", "needs_review"):
        RegistryEntry(
            priors=frozenset({"ready"}),
            kind="inline",
            guard_text='status == "ready"',
        ),
    ("api/routes/draft_room.py", "_sync_enqueue_compile", "drafts", "queued"):
        RegistryEntry(
            priors=MIRRORED_ALLOWLISTS["_COMPILE_ALLOWED_PRIOR_STATUSES"][1],
            kind="allowlist",
            guard_text="_COMPILE_ALLOWED_PRIOR_STATUSES",
            # 'ready' is not a direct edge to queued: the same function first
            # writes ready -> needs_review (the Ready invalidation above),
            # and needs_review -> queued is a legal edge.
            composite={
                "ready": "same_function_predecessor:_sync_enqueue_compile:needs_review",
            },
        ),
    ("api/routes/draft_room.py", "_sync_apply_finding", "draft_findings", "applied"):
        RegistryEntry(
            priors=frozenset({"open"}),
            kind="inline",
            guard_text='if finding_status != "open"',
        ),
    ("api/routes/draft_room.py", "_sync_apply_finding", "drafts", "needs_review"):
        RegistryEntry(
            priors=MIRRORED_ALLOWLISTS["_APPLY_FINDING_ALLOWED_PRIOR_STATUSES"][1],
            kind="allowlist",
            guard_text="_APPLY_FINDING_ALLOWED_PRIOR_STATUSES",
        ),
    ("api/routes/draft_room.py", "_sync_mark_ready", "drafts", "ready"):
        RegistryEntry(
            priors=frozenset({"needs_review"}),
            kind="inline",
            guard_text='!= "needs_review"',
        ),
    # ── services/draft_job_processor.py ──────────────────────────────────
    ("services/draft_job_processor.py", "_fail_compile_job_sync", "drafts", "failed"):
        RegistryEntry(
            priors=frozenset({"queued", "running"}),
            kind="inline",
            guard_text='in ("queued", "running")',
        ),
    ("services/draft_job_processor.py", "_schedule_compile_retry_sync", "drafts", "queued"):
        RegistryEntry(
            priors=frozenset({"failed"}),
            kind="inline",
            guard_text='!= "failed"',
        ),
    # The four _commit_success terminal settles: the documented byte-identical
    # in-transaction replacement of store.set_input_parse_status('ready') +
    # set_job_status('completed') (see the comment in that function). Priors:
    # the job was claimed (pending -> running through DraftStore.claim_next_*)
    # and the input was moved to parsing (store.set_input_parse_status).
    ("services/draft_job_processor.py", "_commit_success", "draft_inputs", "cancelled"):
        RegistryEntry(priors=frozenset({"parsing"}), kind="chain"),
    ("services/draft_job_processor.py", "_commit_success", "draft_jobs", "cancelled"):
        RegistryEntry(priors=frozenset({"running"}), kind="chain"),
    ("services/draft_job_processor.py", "_commit_success", "draft_inputs", "ready"):
        RegistryEntry(priors=frozenset({"parsing"}), kind="chain"),
    ("services/draft_job_processor.py", "_commit_success", "draft_jobs", "completed"):
        RegistryEntry(priors=frozenset({"running"}), kind="chain"),
    # ── services/draft_pipeline.py ───────────────────────────────────────
    ("services/draft_pipeline.py", "_db_publish_revision", "drafts", "needs_review"):
        RegistryEntry(
            priors=MIRRORED_ALLOWLISTS["_ASSEMBLE_ALLOWED_PRIOR_DRAFT_STATUSES"][1],
            kind="allowlist",
            guard_text="_ASSEMBLE_ALLOWED_PRIOR_DRAFT_STATUSES",
            # 'queued' is not a direct edge to needs_review: the compile claim
            # (DraftStore.claim_next_compile_job -> _mark_draft_running_locked)
            # moves the draft queued -> running first, and running ->
            # needs_review is a legal edge.
            composite={
                "queued": "store_claim_moves_prior:queued:running",
            },
        ),
    # Parameterized terminal write: _move_draft's target is constrained to
    # the two terminal compile outcomes by an inline literal guard, and its
    # priors are guarded inline as ("queued", "running").
    ("services/draft_pipeline.py", "_move_draft", "drafts", "?"):
        RegistryEntry(
            priors=frozenset({"queued", "running"}),
            kind="inline",
            guard_text='not in ("failed", "cancelled")',
            composite={},
            targets=frozenset({"failed", "cancelled"}),
        ),
}


@dataclass
class StatusWrite:
    rel_path: str
    function: str
    lineno: int
    table: str
    column: str
    status: str  # literal (without quotes) or "?" for a bound parameter


_UPDATE_RE = re.compile(
    r"\bUPDATE(?:\s+OR\s+(?:IGNORE|REPLACE|ABORT|FAIL|ROLLBACK))?"
    r"(?>\s+|/\*.*?\*/|--[^\n]*)+\s*"
    r'("[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|[A-Za-z_][A-Za-z0-9_]*)'
    r"(?:\s*\.\s*"
    r'("[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|[A-Za-z_][A-Za-z0-9_]*)'
    r")?\s+SET\s+(.*?)(?=\s+WHERE\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_STATUS_COL_RE = re.compile(r"\b(status|parse_status)\s*=\s*('([a-z_]+)'|\?)", re.I)


def _canonical_table(raw: str) -> str:
    """Normalize a captured table reference to its bare lower-case name."""
    parts = re.split(r"\s*\.\s*", raw.strip())
    last = parts[-1].strip()
    if last[:1] in ('"', "`", "[") and len(last) >= 2:
        last = last[1:-1]
    return last.lower()


def _fold(node: ast.AST):
    """Best-effort static SQL string (census fold semantics)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("\x00")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return None if left is None or right is None else left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _fold(node.func.value)
    return None


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()

    def first(body):
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))

    first(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first(node.body)
    return ids


def scan_status_writes(source: str, rel_path: str) -> list[StatusWrite]:
    """Every status-column assignment in a SET clause against the four tables.

    WHERE-clause ``status`` predicates are NOT writes: the regex stops the SET
    capture at WHERE, so ``UPDATE ... WHERE status = 'running'`` never yields
    a StatusWrite (pipeline :3066/:3112 shape). Fails closed on parse errors.
    """
    tree = ast.parse(source, filename=rel_path)
    skip = _docstring_ids(tree)
    sites: list[StatusWrite] = []

    def visit(node: ast.AST, fn: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if id(node) in skip:
            return
        folded = None
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
            folded = _fold(node)
        if folded:
            for um in _UPDATE_RE.finditer(folded):
                raw_table = um.group(1)
                if um.group(2):
                    raw_table = f"{raw_table}.{um.group(2)}"
                table = _canonical_table(raw_table)
                if table not in TRANSITION_MAPS:
                    continue
                for sm in _STATUS_COL_RE.finditer(um.group(3)):
                    literal = sm.group(3)
                    sites.append(
                        StatusWrite(
                            rel_path=rel_path,
                            function=fn,
                            lineno=getattr(node, "lineno", 0),
                            table=table,
                            column=sm.group(1).lower(),
                            status=literal if literal else "?",
                        )
                    )
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return sites


def _module_source(rel_path: str) -> str:
    return (_BACKEND_APP / rel_path).read_text(encoding="utf-8-sig")


def _function_source(source: str, function: str) -> str:
    """Source text of one function (for inline-guard location asserts)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            lines = source.splitlines()
            return "\n".join(lines[node.lineno - 1: node.end_lineno])
    return ""


def _store_source() -> str:
    return _module_source("services/draft_store.py")


def _processor_source() -> str:
    return _module_source("services/draft_job_processor.py")


def validate_sites(sites: list[StatusWrite]) -> list[str]:
    """Fail-closed resolution of every scanned status write.

    Returns a list of human-readable failures; empty means every write
    resolves to a registered transition source with legal edges.
    """
    failures: list[str] = []

    # Mirrored allowlists must exist with exactly the registered values.
    for constant, (module, expected) in MIRRORED_ALLOWLISTS.items():
        src = _module_source(module)
        found = re.search(
            rf"{constant}: frozenset\[str\] = frozenset\(\s*\{{([^}}]*)\}}\s*\)",
            src,
        )
        if not found:
            failures.append(
                f"mirrored allowlist {constant} not found in {module}"
            )
            continue
        actual = frozenset(
            w.strip().strip('"\'') for w in found.group(1).split(",") if w.strip()
        )
        if actual != expected:
            failures.append(
                f"mirrored allowlist {constant} in {module} drifted from the "
                f"registered value: {sorted(actual)} != {sorted(expected)}"
            )

    site_index = {(s.rel_path, s.function, s.table, s.status): s for s in sites}

    for site in sites:
        key = (site.rel_path, site.function, site.table, site.status)
        entry = REGISTRY.get(key)
        if entry is None:
            failures.append(
                f"UNMAPPED status write {site.rel_path}:{site.lineno} in "
                f"{site.function}: {site.table}.{site.column} = "
                f"{site.status!r} does not resolve to a DraftStore transition "
                f"call or a registered mirrored allowlist entry"
            )
            continue
        table_map = TRANSITION_MAPS[site.table]
        if site.status == "?":
            # Parameterized write: resolve through the located literal
            # constraint — no constraint, or any illegal (prior, target)
            # pair, fails closed.
            if not entry.targets:
                failures.append(
                    f"UNCONSTRAINED PARAMETER {site.rel_path}:{site.lineno} in "
                    f"{site.function}: {site.table}.{site.column} = ? has no "
                    f"registered literal constraint"
                )
                continue
            for prior in sorted(entry.priors):
                for target in sorted(entry.targets):
                    if prior == target or target in table_map.get(prior, frozenset()):
                        continue
                    failures.append(
                        f"ILLEGAL EDGE {site.rel_path}:{site.lineno}: "
                        f"parameterized prior {prior!r} -> {target!r} on "
                        f"{site.table} is not a table edge or same-target write"
                    )
            continue
        for prior in sorted(entry.priors):
            justification = entry.composite.get(prior)
            if prior == site.status or site.status in table_map.get(prior, frozenset()):
                continue  # legal direct edge or same-target write
            if justification is None:
                failures.append(
                    f"ILLEGAL EDGE {site.rel_path}:{site.lineno}: prior "
                    f"{prior!r} -> {site.status!r} on {site.table} is neither "
                    f"a table edge, a same-target write, nor a registered "
                    f"composite path"
                )
                continue
            if justification.startswith("same_function_predecessor:"):
                _, fn, lit = justification.split(":", 2)
                if (site.rel_path, fn, site.table, lit) not in site_index:
                    failures.append(
                        f"COMPOSITE BROKEN {site.rel_path}:{site.lineno}: "
                        f"prior {prior!r} relies on predecessor write "
                        f"{fn} -> {lit!r} which is not present"
                    )
            elif justification.startswith("store_claim_moves_prior:"):
                _, prior2, via = justification.split(":", 2)
                store_src = _store_source()
                if (
                    via not in table_map.get(prior2, frozenset())
                    or "_mark_draft_running_locked" not in store_src
                ):
                    failures.append(
                        f"COMPOSITE BROKEN {site.rel_path}:{site.lineno}: "
                        f"prior {prior2!r} relies on the store claim moving it "
                        f"to {via!r}; that edge or the claiming helper is "
                        f"missing from draft_store.py"
                    )
            else:
                failures.append(
                    f"UNKNOWN COMPOSITE JUSTIFICATION for {prior!r} at "
                    f"{site.rel_path}:{site.lineno}"
                )

    # Inline guards must actually exist at their registered functions.
    for (module, function, table, status), entry in REGISTRY.items():
        if entry.kind != "inline" or not entry.guard_text:
            continue
        fn_src = _function_source(_module_source(module), function)
        if entry.guard_text not in fn_src:
            failures.append(
                f"INLINE GUARD MISSING: {module}#{function} (writes "
                f"{table}.{status}) no longer carries its registered guard "
                f"predicate {entry.guard_text!r}"
            )

    # Chain entries: the documented establishers must hold.
    store_src = _store_source()
    processor_src = _processor_source()
    if "def set_input_parse_status" not in store_src:
        failures.append("CHAIN BROKEN: DraftStore.set_input_parse_status is gone")
    if "def set_job_status" not in store_src:
        failures.append("CHAIN BROKEN: DraftStore.set_job_status is gone")
    if "_check_transition" not in _function_source(store_src, "set_input_parse_status"):
        failures.append(
            "CHAIN BROKEN: set_input_parse_status no longer routes through "
            "_check_transition"
        )
    if "_check_transition" not in _function_source(store_src, "set_job_status"):
        failures.append(
            "CHAIN BROKEN: set_job_status no longer routes through "
            "_check_transition"
        )
    if 'store.set_input_parse_status' not in _function_source(
        processor_src, "_move_input_to_parsing"
    ):
        failures.append(
            "CHAIN BROKEN: _move_input_to_parsing no longer calls "
            "store.set_input_parse_status"
        )
    if "store.claim_next_parse_job" not in processor_src or (
        "store.claim_next_compile_job" not in processor_src
    ):
        failures.append(
            "CHAIN BROKEN: the processor no longer claims jobs through the "
            "DraftStore claim methods"
        )

    return failures


class TestStatusTransitionMap(unittest.TestCase):
    longMessage = False

    def _scan_real_modules(self) -> list[StatusWrite]:
        sites: list[StatusWrite] = []
        for rel in CO_OWNER_MODULES:
            sites.extend(scan_status_writes(_module_source(rel), rel))
        return sites

    def test_co_owners_are_mapped(self):
        """C11: every status write in the three co-owner modules resolves."""
        sites = self._scan_real_modules()
        self.assertEqual(
            len(sites),
            13,
            f"expected the 13 registered status writes, found {len(sites)}: "
            f"{[(s.rel_path, s.function, s.table, s.status) for s in sites]}",
        )
        failures = validate_sites(sites)
        self.assertEqual(failures, [], "unresolved/illegal status writes:\n" + "\n".join(failures))
        # Positive asserts for the table-specific literals the plan-critic
        # called out (a cross-table coincidence must be the ONLY way these
        # resolve — they must come from their own tables' maps).
        self.assertIn("completed", _JOB_TRANSITIONS["running"])
        self.assertIn("ready", _INPUT_TRANSITIONS["parsing"])
        index = {(s.rel_path, s.function, s.table, s.status) for s in sites}
        self.assertIn(
            ("services/draft_job_processor.py", "_commit_success", "draft_jobs", "completed"),
            index,
        )
        self.assertIn(
            ("services/draft_job_processor.py", "_commit_success", "draft_inputs", "ready"),
            index,
        )
        # The three composite / same-target shapes are exercised by
        # validate_sites (enqueue 'ready' prior via the same-function
        # predecessor; apply 'needs_review' same-target; assemble 'queued'
        # prior via the store claim) — their REGISTRY rows carry the
        # justifications, and dropping any link fails above.

    def test_unmapped_status_write_fails_closed(self):
        """C10 fail-closed leg: an unregistered write produces a named failure."""
        fake = StatusWrite(
            rel_path="api/routes/draft_room.py",
            function="_sync_enqueue_compile",
            lineno=9999,
            table="drafts",
            column="status",
            status="invented",
        )
        failures = validate_sites([fake])
        self.assertTrue(
            any(
                "UNMAPPED" in f and "invented" in f and "draft_room.py:9999" in f
                for f in failures
            ),
            f"planted unmapped literal must fail by name, got: {failures}",
        )

    def test_planted_unregistered_literal(self):
        """C12: planting an ad-hoc literal in a co-owner copy fails the guard."""
        src = _module_source("api/routes/draft_room.py")
        planted = src.replace(
            "def _sync_enqueue_compile(",
            "def _planted_probe(conn):\n"
            "    conn.execute(\n"
            "        \"UPDATE drafts SET status = 'invented' WHERE id = 1\"\n"
            "    )\n\n\ndef _sync_enqueue_compile(",
            1,
        )
        self.assertNotEqual(planted, src, "plant failed to apply")
        sites = scan_status_writes(planted, "api/routes/draft_room.py")
        hit = [s for s in sites if s.status == "invented"]
        self.assertTrue(hit, "scanner went blind on the planted literal")
        failures = validate_sites([*self._scan_real_modules(), *hit])
        self.assertTrue(
            any("UNMAPPED" in f and "'invented'" in f for f in failures),
            f"planted literal must be named in the failures, got: {failures}",
        )

    def test_planted_unconstrained_parameterized_write_fails_closed(self):
        """A parameterized status write with no located literal constraint
        fails closed (the _move_draft shape WITHOUT its constraining guard)."""
        src = _module_source("services/draft_pipeline.py")
        planted = src.replace(
            "def _move_draft(",
            "def _planted_param(conn, status):\n"
            "    conn.execute(\n"
            "        \"UPDATE drafts SET status = ? WHERE id = 1\"\n"
            "    )\n\n\ndef _move_draft(",
            1,
        )
        self.assertNotEqual(planted, src, "plant failed to apply")
        sites = scan_status_writes(planted, "services/draft_pipeline.py")
        hit = [s for s in sites if s.function == "_planted_param"]
        self.assertTrue(hit, "scanner went blind on the planted parameterized write")
        failures = validate_sites(hit)
        self.assertTrue(
            any(
                "UNMAPPED" in f and "'?'" in f and "_planted_param" in f
                for f in failures
            ),
            f"unconstrained parameterized write must fail closed, got: {failures}",
        )

    def test_where_clause_status_is_not_a_write(self):
        """SET-clause precision: status only in WHERE never yields a site."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "where_only.py"
            p.write_text(
                "import sqlite3\n\n\n"
                "def heartbeat(conn: sqlite3.Connection) -> None:\n"
                "    conn.execute(\n"
                "        \"UPDATE draft_jobs SET heartbeat_at = 1 "
                "WHERE status = 'running'\"\n"
                "    )\n",
                encoding="utf-8",
            )
            sites = scan_status_writes(p.read_text(encoding="utf-8"), "where_only.py")
        self.assertEqual(sites, [])


if __name__ == "__main__":
    unittest.main()
