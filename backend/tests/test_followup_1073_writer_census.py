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
        """Exactly the 8 sanctioned production call sites, no drift."""
        dp_sites = _call_sites(self.dp, "set_enrichment_status")
        bt_sites = _call_sites(self.bt, "set_enrichment_status")
        self.assertEqual(len(dp_sites), 7, f"document_processor sites: {dp_sites}")
        self.assertEqual(len(bt_sites), 1, f"background_tasks sites: {bt_sites}")

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
