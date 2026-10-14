"""Acceptance checks for issue #710 (C05): raw query text in default logs.

The logging contract says user-derived text (the raw query, its transforms,
sub-queries, and rewrites) must never appear in default (INFO+) logs. These
checks freeze the three trace/planner leak sites root-caused for issue #710:

- AC1: ``RAGTrace.log()`` must not emit the text of the five user-derived
  sibling fields that ride along in ``to_dict()`` (``transformed_queries``,
  ``wiki_query``, ``followup_rewrite``, ``query_plan``,
  ``unverifiable_claims``) — only the redacted ``original_query`` form is
  safe today.
- AC2: a FUTURE trace field added to ``to_dict()`` must not flow into the
  log dict by default — ``to_log_dict()`` must allow-list what it logs
  rather than pass through everything ``to_dict()`` returns.
- AC3: ``QueryPlanner.plan()`` must not log the raw query text at INFO on
  the success path (counts are fine; the text itself is not).

RED at base, GREEN after the fix.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.llm_client import LLMClient
from app.services.query_transformer import QueryPlanner
from app.services.rag_trace import RAGTrace

# assertLogs is a TestCase method; these checks are module-level pytest
# functions, so share one bare TestCase instance purely as the context-manager
# provider (same technique as test_issue_296_logging_ssrf.py, which uses
# self.assertLogs inside unittest.TestCase classes).
_ASSERT_LOGS = unittest.TestCase()


def test_trace_log_emits_no_user_derived_field_text():
    """AC1: RAGTrace.log() emits none of the five user-derived field texts.

    RED at base (all five markers ride through to_log_dict() unredacted),
    GREEN after the fix.
    """
    t = RAGTrace(original_query="what is the C05ORIG salary policy")
    t.transformed_queries = ["what is the C05TQ salary policy"]
    t.wiki_query = "how does the C05WQ leave policy work"
    t.followup_rewrite = "what did they mean by C05FR in the last answer"
    t.query_plan = ["compare the C05QP storage engines", "auth model overview"]
    t.unverifiable_claims = ["the C05UC claim has no supporting citation"]

    markers = ("C05TQ", "C05WQ", "C05FR", "C05QP", "C05UC")

    with _ASSERT_LOGS.assertLogs("app.services.rag_trace", level="INFO") as cm:
        t.log()

    joined = "\n".join(cm.output)
    count = sum(1 for marker in markers if marker in joined)
    assert count == 0, (
        f"{count} user-derived trace field marker(s) leaked into the INFO "
        f"log: {joined}"
    )


def test_unknown_trace_field_excluded_from_log_by_default():
    """AC2: a future to_dict() field must not flow into the log dict.

    Patches ``RAGTrace.to_dict`` to also return a hypothetical future
    text-bearing field; ``log()`` must not emit it. RED at base (the
    unknown field passes straight through to_log_dict()), GREEN after the
    fix.
    """
    original_to_dict = RAGTrace.to_dict

    def _to_dict_with_future_field(self):
        data = original_to_dict(self)
        data["c05_future_text_field"] = "C05FUTURE marker text"
        return data

    t = RAGTrace(original_query="some ordinary question")

    with patch.object(RAGTrace, "to_dict", _to_dict_with_future_field):
        with _ASSERT_LOGS.assertLogs("app.services.rag_trace", level="INFO") as cm:
            t.log()

    joined = "\n".join(cm.output)
    count = joined.count("C05FUTURE")
    assert count == 0, (
        f"unknown trace field leaked into the INFO log {count} time(s): "
        f"{joined}"
    )


async def test_query_planner_success_logs_no_query_text_at_info():
    """AC3: QueryPlanner.plan() success path logs no raw query text at INFO.

    RED at base (the success-path INFO record interpolates query[:60]),
    GREEN after the fix. DEBUG records may still carry the text — the
    capture is at INFO level only.
    """
    client = MagicMock(spec=LLMClient)
    client.chat_completion = AsyncMock(
        return_value='["storage engine C05PLAN", "auth model"]'
    )
    planner = QueryPlanner(client)

    with _ASSERT_LOGS.assertLogs(
        "app.services.query_transformer", level="INFO"
    ) as cm:
        # Non-simple (comparative, conjoined) query -> the LLM path runs.
        await planner.plan(
            "compare the C05PLAN storage engines and the auth model"
        )

    count = sum(1 for rec in cm.records if "C05PLAN" in rec.getMessage())
    assert count == 0, (
        f"{count} INFO+ planner record(s) carry raw query text: "
        f"{cm.output}"
    )
