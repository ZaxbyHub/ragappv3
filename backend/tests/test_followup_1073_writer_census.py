"""Phase 4.2 guardrail: enrichment-writer census + truncation-literal census.

Issues #1075/#1076. RED at base, GREEN after the fix. This file pins the
WRITER ROSTER and the GUARD SHAPES so any future terminal
``files.enrichment_status`` write (or truncation-token literal comparison)
must be consciously dispositioned before it can land.

Named mutations that turn this RED (proven in Phase 4 evidence):
- drop ``enrichment_error IS NULL`` from the atom-sync UPDATE;
- drop ``require_file_hash`` from writer 8's call;
- revert chunk_enrichment's finish_reason guard to ``== "length"``.
"""

import ast
import io
import re
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
DP = APP_ROOT / "services" / "document_processor.py"
BT = APP_ROOT / "services" / "background_tasks.py"
CE = APP_ROOT / "services" / "chunk_enrichment.py"
CA = APP_ROOT / "api" / "routes" / "canvas.py"
LC = APP_ROOT / "services" / "llm_client.py"
ES = APP_ROOT / "services" / "enrichment_state.py"


def _src(path: Path) -> str:
    return io.open(path, encoding="utf-8").read()


def _call_sites(source: str, func: str) -> list[tuple[int, dict]]:
    """Every ``self.<func>(...)`` call with its keyword args (AST)."""
    sites = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == func:
                sites.append(
                    (
                        node.lineno,
                        {
                            kw.arg: ast.unparse(kw.value)
                            for kw in node.keywords
                            if kw.arg
                        },
                    )
                )
    return sites


class TestEnrichmentWriterCensus(unittest.TestCase):
    """Class A: terminal enrichment-status writes carry atomic guards."""

    def setUp(self) -> None:
        self.dp = _src(DP)
        self.bt = _src(BT)

    def test_set_enrichment_status_call_roster_is_pinned(self):
        """Exactly the 8 sanctioned production call sites repo-wide, no drift."""
        total = 0
        for path in APP_ROOT.rglob("*.py"):
            sites = _call_sites(_src(path), "set_enrichment_status")
            if sites:
                total += len(sites)
        self.assertEqual(total, 8, "set_enrichment_status roster drifted")

    def test_six_terminal_writers_carry_the_hash_currency_guard(self):
        """All four ``complete`` + both ``error`` writers pass require_file_hash.

        The entry stamps (pending/processing) must NOT pass it: the processing
        stamp's return value is consumed as ``processing_started_at``.
        """
        dp_sites = _call_sites(self.dp, "set_enrichment_status")
        guarded, entry = [], []
        for line, kwargs in dp_sites:
            if "require_file_hash" in kwargs:
                guarded.append((line, kwargs["require_file_hash"]))
            else:
                entry.append(line)
        self.assertEqual(
            len(guarded), 6, f"expected 6 guarded terminal writers: {guarded}"
        )
        self.assertEqual(len(entry), 1, f"expected 1 entry stamp: {entry}")

    def test_guarded_update_carries_the_full_currency_predicate(self):
        """The guarded WHERE is the FULL hash/status predicate, verbatim, and
        is the FINAL ``where`` assignment before the execute.

        Killing mutations: dropping only ``AND status IN ('indexed',
        'partial')`` (hash-only guard), OR re-assigning ``where`` after the
        guarded construction (override), must turn this RED.
        """
        m = re.search(
            r"require_file_hash is not None:(.*?)try:", self.dp, re.S
        )
        self.assertIsNotNone(m, "require_file_hash WHERE construction not found")
        guarded = m.group(1)
        self.assertIn("AND file_hash = ?", guarded)
        self.assertIn("AND status IN ('indexed', 'partial')", guarded)
        # Final-assignment semantics, scoped to the method body: the LAST
        # `where = ` assignment in set_enrichment_status must be the guarded
        # construction (an override would silently drop the guard).
        method = re.search(
            r"async def set_enrichment_status(.*?)(?=\n    (?:async )?def )",
            self.dp,
            re.S,
        )
        self.assertIsNotNone(method, "set_enrichment_status body not found")
        body = method.group(1)
        last_where = body.rfind("where = ")
        guarded_at = body.find("require_file_hash is not None:")
        self.assertGreater(
            last_where, guarded_at,
            "where is reassigned after the guarded construction — the full "
            "predicate is not the final value used by the UPDATE",
        )
        self.assertIn("AND status IN ('indexed', 'partial')", body[last_where:])

    def test_complete_write_is_one_statement_not_read_then_write(self):
        """The complete branch's CASE feeds the UPDATE in the SAME statement,
        with exactly ONE status_sql assignment (no override).

        Killing mutations: a read-then-write split, OR keeping the CASE
        assignment and then overwriting status_sql from a separately-read
        aggregate, must turn this RED — the atomicity claim is precisely that
        there is no Python seam between the aggregate decision and the write.
        """
        m = re.search(
            r'status_sql = "\?"(.*?)if require_file_hash is not None:',
            self.dp,
            re.S,
        )
        self.assertIsNotNone(m, "complete-branch status_sql construction missing")
        branch = m.group(1)
        self.assertIn('status == "complete"', self.dp)
        self.assertIn('status_sql = "(" + aggregate_status_case_sql() + ")"', branch)
        # Exactly one status_sql assignment in the branch, and no read of the
        # aggregate into a local that could be spliced in later.
        self.assertEqual(
            branch.count("status_sql ="), 1,
            "status_sql is assigned more than once — a later assignment can "
            "override the inline CASE (read-then-write seam)",
        )
        self.assertNotIn("SELECT " + "aggregate", branch.replace('aggregate_status_case_sql() + ")"', ""))
        self.assertNotIn("SELECT enrichment_status", branch)

    def test_cancel_cas_pins_both_predicate_halves(self):
        """The stale/cancel CAS WHERE carries status AND timestamp, verbatim.

        Killing mutation: dropping only ``AND enrichment_status =
        'processing'`` (timestamp-only CAS) must turn this RED.
        """
        m = re.search(
            r"async def _mark_enrichment_stale_if_current_job(.*?)except sqlite3\.Error",
            self.dp,
            re.S,
        )
        self.assertIsNotNone(m, "stale/cancel CAS not found")
        body = m.group(1)
        self.assertIn("AND enrichment_status = 'processing'", body)
        self.assertIn("AND enrichment_updated_at = ?", body)

    def test_atom_sync_update_carries_the_chunk_failure_predicate(self):
        """``_sync_file_enrichment_status`` skips rows with a chunk failure."""
        m = re.search(
            r"def _sync_file_enrichment_status(.*?)conn\.commit\(\)",
            self.bt,
            re.S,
        )
        self.assertIsNotNone(m, "atom sync body not found")
        sql = m.group(1)
        self.assertIn("enrichment_error IS NULL", sql)
        self.assertIn("aggregate_status_case_sql()", sql)
        # Single statement: decision (CASE) and write are one UPDATE.
        self.assertIn("UPDATE files", sql)

    def test_complete_branch_uses_the_shared_case_in_the_same_statement(self):
        """``set_enrichment_status`` computes 'complete' via the shared CASE."""
        self.assertIn("aggregate_status_case_sql()", self.dp)
        self.assertIn(
            "status == \"complete\"", self.dp, "complete branch dispatch missing"
        )

    def test_cancel_handler_uses_the_attempt_stamp_cas(self):
        """run_enrichment_job's CancelledError task calls the CAS with its message."""
        m = re.search(
            r"async def run_enrichment_job.*?except asyncio\.CancelledError:(.*?)\n            raise\n",
            self.dp,
            re.S,
        )
        self.assertIsNotNone(m, "run_enrichment_job cancel handler not found")
        body = m.group(1)
        self.assertIn("_mark_enrichment_stale_if_current_job", body)
        self.assertIn("error_message=", body)
        self.assertNotIn(
            "set_enrichment_status", body, "cancel path must not use the raw writer"
        )

    def test_sql_update_roster_is_pinned(self):
        """Every string literal updating files.enrichment_status is sanctioned."""
        hits = []
        for label, src in (("dp", self.dp), ("bt", self.bt)):
            for lit in re.findall(r'"""(.*?)"""|"(.*?)"', src, re.S):
                text = lit[0] or lit[1]
                if "UPDATE files" in text and "enrichment_status" in text:
                    hits.append(label)
        # dp: the generalized writer (incl. complete CASE) + the stale/cancel
        # CAS + the re-ingest NULL reset; bt: recovery sweep x2 arms + atom
        # sync. Any sixth-or-newer shape must be consciously dispositioned.
        self.assertEqual(len(hits), 6, f"UPDATE files roster drifted: {hits}")


class TestTruncationTokenCensus(unittest.TestCase):
    """Class B: no production literal finish_reason comparison survives."""

    def test_no_literal_finish_reason_comparison_in_production(self):
        offenders = []
        for path in APP_ROOT.rglob("*.py"):
            src = _src(path)
            for m in re.finditer(r"finish_reason[^\n]*==\s*[\"']", src):
                offenders.append(f"{path.relative_to(APP_ROOT)}:{src[:m.start()].count(chr(10))+1}")
        self.assertEqual(offenders, [], f"literal finish_reason comparisons: {offenders}")

    def test_both_guards_use_the_shared_set(self):
        ce_src, ca_src = _src(CE), _src(CA)
        self.assertIn("TRUNCATION_FINISH_REASONS", ce_src)
        self.assertIn("TRUNCATION_FINISH_REASONS", ca_src)
        lc_src = _src(LC)
        self.assertIn(
            'frozenset({"length", "max_tokens", "max_length"})', lc_src
        )

    def test_shared_case_is_correlated_on_the_outer_file_row(self):
        es_src = _src(ES)
        self.assertIn("s.file_id = files.id", es_src)
        self.assertIn("atom_id IS NOT NULL", es_src)


if __name__ == "__main__":
    unittest.main()
