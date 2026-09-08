"""Tests for the optional total prompt budget in PromptBuilderService (issue
#511 FULL-ENH-01, AC7).

Contract:
- ``prompt_budget_enabled=False`` (default): assembly is byte-identical to
  the pre-budget logic — full section set, last-20 history window, no
  omission note, ``last_budget_report`` is None.
- ``prompt_budget_enabled=True``: the assembled prompt (system + history +
  evidence + memories + wiki + kms + query) plus the reserved answer tokens
  fits ``model_context_tokens - prompt_reserve_output_tokens``; overflow is
  shed lowest-value-first (history oldest-first keeping the newest 2 ->
  supporting tail -> wiki/kms long entries -> memories longest-first except
  short facts -> primary beyond top-3); system prompt, user query, top-3
  primary evidence, and short facts (<= 120 chars) are NEVER shed; an
  in-prompt omission note plus ``last_budget_report`` record what was shed;
  surviving items keep their positional labels.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.prompt_builder import PromptBuilderService
from app.services.token_accounting import count_tokens

FILLER = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu "
    "xi omicron pi rho sigma tau upsilon phi chi psi omega "
)

SYSTEM_PROMPT = (
    "SYSTEM-INSTRUCTIONS-MARKER-1 You are a grounded assistant. Answer only "
    "from the provided context and cite the labeled sources."
)
QUERY = "USER-QUERY-MARKER-99 What do the retrieved sources conclude?"
SHORT_MEMORY = "SHORT-MEMORY-FACT-42 the archive mirror rotates every Tuesday."
LONG_MEMORY = "LONG-MEMORY-FACT-1 detailed retention policy notes: " + FILLER * 12
SHORT_WIKI = "SHORT-WIKI-FACT-77 the north gate closes at six."
LONG_WIKI = "LONG-WIKI-CLAIM-1 " + FILLER * 30
SHORT_KMS = "SHORT-KMS-FACT-88 backups run nightly at 2am."
LONG_KMS = "LONG-KMS-EXCERPT-1 " + FILLER * 20

OMISSION_WORDS = ("omit", "truncat", "dropped", "shed", "trimmed", "budget")


@dataclass
class Chunk:
    text: str
    file_id: str
    score: float
    metadata: dict = field(default_factory=dict)
    parent_window_text: object = None


def make_chunk(idx: int) -> Chunk:
    return Chunk(
        text=f"CHUNK-{idx:02d}-MARKER payload: " + FILLER * 8,
        file_id=f"file{idx:02d}",
        score=round(0.95 - idx * 0.01, 2),
        metadata={"source_file": f"doc{idx:02d}.pdf"},
    )


def make_history(n: int = 30):
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"HISTORY-TURN-{i:02d}-MARKER exchange {i} with " + FILLER,
        }
        for i in range(n)
    ]


def make_wiki():
    return [
        SimpleNamespace(
            title="Long wiki page",
            page_type="claim",
            confidence=0.9,
            claim_status="verified",
            page_status="",
            provenance_summary="3 sources",
            claim_text=LONG_WIKI,
            excerpt="",
        ),
        SimpleNamespace(
            title="Short wiki page",
            page_type="claim",
            confidence=0.8,
            claim_status="verified",
            page_status="",
            provenance_summary="1 source",
            claim_text=SHORT_WIKI,
            excerpt="",
        ),
    ]


def make_kms():
    return [
        SimpleNamespace(
            title="Long KMS entry",
            status="active",
            source_type="runbook",
            excerpt=LONG_KMS,
            summary="",
        ),
        SimpleNamespace(
            title="Short KMS entry",
            status="active",
            source_type="runbook",
            excerpt="",
            summary=SHORT_KMS,
        ),
    ]


def make_memories():
    return [
        SimpleNamespace(content=LONG_MEMORY),
        SimpleNamespace(content=SHORT_MEMORY),
    ]


@pytest.fixture
def scenario():
    """All scenario inputs + a builder under budget-neutral settings."""
    return {
        "chunks": [make_chunk(i) for i in range(12)],
        "history": make_history(30),
        "wiki": make_wiki(),
        "kms": make_kms(),
        "memories": make_memories(),
    }


def _configure(
    monkeypatch,
    *,
    enabled=False,
    model_context_tokens=8192,
    reserve=2048,
):
    monkeypatch.setattr(settings, "prompt_budget_enabled", enabled)
    monkeypatch.setattr(settings, "model_context_tokens", model_context_tokens)
    monkeypatch.setattr(
        settings, "prompt_reserve_output_tokens", reserve
    )
    monkeypatch.setattr(settings, "primary_evidence_count", 0)
    monkeypatch.setattr(settings, "anchor_best_chunk", False)
    monkeypatch.setattr(settings, "parent_retrieval_enabled", False)
    monkeypatch.setattr(settings, "context_max_tokens", 6000)
    monkeypatch.setattr(settings, "max_context_chunks", 12)
    return PromptBuilderService(system_prompt=SYSTEM_PROMPT)


def _build(builder, sc):
    return builder.build_messages(
        user_input=QUERY,
        chat_history=sc["history"],
        chunks=sc["chunks"],
        memories=sc["memories"],
        wiki_evidence=sc["wiki"],
        kms_evidence=sc["kms"],
    )


def _joined(messages) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


def _floor_tokens(sc, monkeypatch) -> int:
    """Token cost of the never-shed floor assembly (computed, not hardcoded).

    Neutralizes the ancillary flags first (the real defaults enable the
    anchor chunk and parent-window rendering, which are not part of the
    scenario), and uses the same counter the budget pass uses, so the
    derived budget is exact under both the tiktoken path and the
    conservative fallback.
    """
    _configure(monkeypatch, enabled=False)
    builder = PromptBuilderService(system_prompt=SYSTEM_PROMPT)
    floor_msgs = builder.build_messages(
        user_input=QUERY,
        chat_history=sc["history"][-2:],  # newest-2 history floor
        chunks=sc["chunks"][:3],  # top-3 primary floor
        memories=[SimpleNamespace(content=SHORT_MEMORY)],
        wiki_evidence=[sc["wiki"][1]],  # short wiki entry only
        kms_evidence=[sc["kms"][1]],  # short kms entry only
    )
    return count_tokens(_joined(floor_msgs))


class TestBudgetDisabled:
    """Flag off (default): today's assembly, byte for byte."""

    def test_full_section_set_preserved(self, monkeypatch, scenario):
        builder = _configure(monkeypatch, enabled=False)
        messages = _build(builder, scenario)
        text = _joined(messages)

        # Every input section is present exactly as before.
        for i in range(12):
            assert f"CHUNK-{i:02d}-MARKER" in text
        for marker in ("[W1]", "[W2]", "[K1]", "[K2]", "[M1]", "[M2]"):
            assert marker in text
        assert "Primary Evidence:" in text
        assert "Supporting Evidence:" in text
        assert "Wiki Evidence" in text
        assert "Knowledge Base Evidence" in text
        assert "Memories:" in text
        assert QUERY in text
        assert SYSTEM_PROMPT in messages[0]["content"]

        # Last-20 history window (pre-budget behavior): turns 10..29 in,
        # turns 0..9 out.
        for i in range(10, 30):
            assert f"HISTORY-TURN-{i:02d}-MARKER" in text
        for i in range(0, 10):
            assert f"HISTORY-TURN-{i:02d}-MARKER" not in text

        # Message structure: system, 20 history, user.
        assert messages[0]["role"] == "system"
        assert len(messages) == 22
        assert messages[-1]["role"] == "user"

        # No omission note.
        for word in OMISSION_WORDS:
            assert word not in text.lower(), f"unexpected note word: {word}"

    def test_budget_on_with_ample_budget_is_byte_identical(
        self, monkeypatch, scenario
    ):
        """With a huge budget nothing is shed and the output equals the
        flag-off output message-for-message."""
        off = _configure(monkeypatch, enabled=False)
        msgs_off = _build(off, scenario)

        on = _configure(
            monkeypatch, enabled=True, model_context_tokens=10_000_000
        )
        msgs_on = _build(on, scenario)

        assert msgs_on == msgs_off

    def test_report_is_none_when_disabled(self, monkeypatch, scenario):
        builder = _configure(monkeypatch, enabled=False)
        _build(builder, scenario)
        assert builder.last_budget_report is None


class TestBudgetEnabled:
    """Flag on: fit, shed priorities, note, report, label stability."""

    def test_prompt_fits_budget(self, monkeypatch, scenario):
        total, reserve = 1200, 256
        builder = _configure(
            monkeypatch,
            enabled=True,
            model_context_tokens=total,
            reserve=reserve,
        )
        messages = _build(builder, scenario)
        prompt_tokens = count_tokens(_joined(messages))
        assert prompt_tokens + reserve <= total

    def test_system_query_and_top_primary_survive(self, monkeypatch, scenario):
        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=1200, reserve=256
        )
        messages = _build(builder, scenario)
        text = _joined(messages)
        assert SYSTEM_PROMPT in messages[0]["content"]
        assert QUERY in text
        # The TOP primary evidence chunk is never shed ([S1] / CHUNK-00).
        assert "CHUNK-00-MARKER" in text
        assert "[S1]" in text

    def test_omission_note_and_report(self, monkeypatch, scenario):
        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=1200, reserve=256
        )
        messages = _build(builder, scenario)
        text = _joined(messages)
        assert "omit" in text.lower(), "in-prompt omission note missing"

        report = builder.last_budget_report
        assert report is not None
        assert report["enabled"] is True
        assert report["budget_tokens"] == 1200 - 256
        assert report["prompt_tokens"] == count_tokens(text)
        for key in (
            "shed_history",
            "shed_supporting",
            "shed_wiki",
            "shed_kms",
            "shed_memories",
            "shed_primary",
        ):
            assert isinstance(report[key], int)
        assert report["shed_history"] > 0
        assert report["shed_supporting"] > 0

    def test_no_note_and_zero_report_when_it_fits(self, monkeypatch, scenario):
        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=10_000_000
        )
        messages = _build(builder, scenario)
        text = _joined(messages)
        for word in OMISSION_WORDS:
            assert word not in text.lower()
        report = builder.last_budget_report
        assert report is not None
        assert report["enabled"] is True
        assert report["prompt_tokens"] == count_tokens(text)
        assert report["shed_history"] == 0
        assert report["shed_supporting"] == 0
        assert report["shed_wiki"] == 0
        assert report["shed_kms"] == 0
        assert report["shed_memories"] == 0
        assert report["shed_primary"] == 0

    def test_short_facts_retained_long_items_shed(self, monkeypatch, scenario):
        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=1200, reserve=256
        )
        messages = _build(builder, scenario)
        text = _joined(messages)
        assert "SHORT-MEMORY-FACT-42" in text
        assert "SHORT-WIKI-FACT-77" in text
        assert "SHORT-KMS-FACT-88" in text
        # Long counterparts were the shed candidates.
        assert "LONG-MEMORY-FACT-1" not in text
        assert "LONG-WIKI-CLAIM-1" not in text
        assert "LONG-KMS-EXCERPT-1" not in text

    def test_wiki_label_positional_after_shed(self, monkeypatch, scenario):
        """The long wiki entry [W1] is shed; the short entry keeps [W2] —
        surviving labels are never renumbered."""
        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=1200, reserve=256
        )
        messages = _build(builder, scenario)
        text = _joined(messages)
        assert "[W2]" in text
        assert "[W1]" not in text
        assert "SHORT-WIKI-FACT-77" in text

    def test_top3_protected_when_budget_allows(self, monkeypatch, scenario):
        """Budget = never-shed floor + slack: tiers 1-6 exhaust exactly, the
        full top-3 primary evidence survives with the short facts."""
        floor = _floor_tokens(scenario, monkeypatch)
        # +80 covers the omission note under either counter while staying
        # below the cheapest remaining sheddable section (~250 tokens), so
        # the ladder must consume ALL of tiers 1-6 and never reach the
        # last-resort tier.
        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=floor + 80, reserve=0
        )
        messages = _build(builder, scenario)
        text = _joined(messages)

        for i in range(3):
            assert f"CHUNK-{i:02d}-MARKER" in text
        for i in range(3, 12):
            assert f"CHUNK-{i:02d}-MARKER" not in text
        assert "HISTORY-TURN-28-MARKER" in text  # newest-2 floor
        assert "HISTORY-TURN-29-MARKER" in text
        assert "SHORT-MEMORY-FACT-42" in text
        assert "SHORT-WIKI-FACT-77" in text
        assert "SHORT-KMS-FACT-88" in text

        report = builder.last_budget_report
        # 30 history -> last-20 window; newest 2 protected so up to 18 shed.
        assert 0 < report["shed_history"] <= 18
        assert report["shed_supporting"] == 7  # 12 chunks - 5 primary
        assert report["shed_wiki"] == 1  # long entry only
        assert report["shed_kms"] == 1
        assert report["shed_memories"] == 1  # long memory only
        assert report["shed_primary"] == 2  # primary chunks 4 and 5

    def test_last_resort_pierces_top3_but_never_top1(self, monkeypatch, scenario):
        """Budget exactly at the floor: the last-resort tier sheds primary #3
        (then #2 if still needed) but NEVER the top primary chunk, and short
        facts survive even then (frozen acceptance check C7 semantics)."""
        floor = _floor_tokens(scenario, monkeypatch)
        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=floor, reserve=0
        )
        messages = _build(builder, scenario)
        text = _joined(messages)

        # Top primary chunk and the short facts are inviolable.
        assert "CHUNK-00-MARKER" in text
        assert "[S1]" in text
        assert "SHORT-MEMORY-FACT-42" in text
        assert "SHORT-WIKI-FACT-77" in text
        # The floor assembly (top-3 + shorts + note) exceeds the exact-floor
        # budget, so the last-resort tier fired: #3 at minimum is gone.
        assert "CHUNK-02-MARKER" not in text
        report = builder.last_budget_report
        assert report["shed_primary"] >= 3  # 2 normal + at least #3

    def test_history_shed_before_supporting(self, monkeypatch, scenario):
        """A budget that the history alone exceeds: history is shed while
        supporting evidence is untouched."""
        # Cost of the assembly with the newest-2 history floor but ALL other
        # sections present:
        builder = PromptBuilderService(system_prompt=SYSTEM_PROMPT)
        no_hist_msgs = builder.build_messages(
            user_input=QUERY,
            chat_history=[],
            chunks=scenario["chunks"],
            memories=scenario["memories"],
            wiki_evidence=scenario["wiki"],
            kms_evidence=scenario["kms"],
        )
        budget = count_tokens(_joined(no_hist_msgs)) + 20

        b = _configure(
            monkeypatch, enabled=True, model_context_tokens=budget, reserve=0
        )
        messages = _build(b, scenario)

        report = b.last_budget_report
        assert report["shed_history"] > 0
        assert report["shed_supporting"] == 0
        assert report["shed_wiki"] == 0
        assert report["shed_kms"] == 0
        assert report["shed_memories"] == 0
        assert report["shed_primary"] == 0
        # All 12 chunk markers still present.
        for i in range(12):
            assert f"CHUNK-{i:02d}-MARKER" in _joined(messages)

    def test_single_info_log_line_on_shed(
        self, monkeypatch, scenario, caplog
    ):
        import logging

        builder = _configure(
            monkeypatch, enabled=True, model_context_tokens=1200, reserve=256
        )
        with caplog.at_level(logging.INFO, logger="app.services.prompt_builder"):
            _build(builder, scenario)
        shed_logs = [
            r
            for r in caplog.records
            if "prompt budget" in r.getMessage().lower()
        ]
        assert len(shed_logs) == 1
        # Counts only — no user content leaks into the log line.
        assert QUERY not in shed_logs[0].getMessage()
