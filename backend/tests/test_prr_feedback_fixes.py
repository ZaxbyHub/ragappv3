"""Regression tests for the PR #528 swarm-pr-review fixes (PRR-001/005/010).

These append to the existing suites' patterns:
- PRR-001: SettingsUpdate rejects absurd numeric values for the four new
  issue-#511 numeric settings (redis_io_timeout_seconds=0 caused an
  instant-timeout cache-miss storm; context_distiller_max_sentences<=0
  disabled the O(n^2) dedup cap).
- PRR-005: the repeated-anchor passage is sheddable as the budget ladder's
  last resort, so a tiny budget cannot return an over-budget prompt.
- PRR-010: _safe_sigmoid maps NaN logits to 0.0 instead of propagating NaN.
"""

import math

import pytest
from pydantic import ValidationError

from app.api.routes.settings import SettingsUpdate
from app.services.prompt_builder import PromptBuilderService
from app.services.reranking import _safe_sigmoid


class TestPRR001NumericSettingValidators:
    """PUT /settings persists via setattr bypassing model validation, so
    SettingsUpdate field validators are the only guard on the write path."""

    @pytest.mark.parametrize(
        ("field", "bad"),
        [
            ("model_context_tokens", 0),
            ("model_context_tokens", -100),
            ("prompt_reserve_output_tokens", -1),
            ("redis_io_timeout_seconds", 0),
            ("redis_io_timeout_seconds", -0.5),
            ("context_distiller_max_sentences", 0),
            ("context_distiller_max_sentences", -10),
        ],
    )
    def test_absurd_values_rejected(self, field, bad):
        with pytest.raises(ValidationError):
            SettingsUpdate(**{field: bad})

    @pytest.mark.parametrize(
        ("field", "good"),
        [
            ("model_context_tokens", 8192),
            ("prompt_reserve_output_tokens", 0),
            ("prompt_reserve_output_tokens", 2048),
            ("redis_io_timeout_seconds", 1.0),
            ("context_distiller_max_sentences", 600),
        ],
    )
    def test_reasonable_values_accepted(self, field, good):
        update = SettingsUpdate(**{field: good})
        assert getattr(update, field) == good

    def test_none_still_allowed(self):
        """Omitted fields stay None (no change on PUT)."""
        update = SettingsUpdate(model_context_tokens=None)
        assert update.model_context_tokens is None


class TestPRR005AnchorSheddable:
    """The repeated-anchor passage must be sheddable (tier 8) so the ladder
    can always reach budget; [S1] itself still survives."""

    def test_anchor_shed_when_tiny_budget(self, monkeypatch):
        from app.config import settings as real_settings_module

        pytest.importorskip("app.services.token_accounting")
        builder_settings = real_settings_module
        monkeypatch.setattr(builder_settings, "prompt_budget_enabled", True)
        monkeypatch.setattr(builder_settings, "model_context_tokens", 300)
        monkeypatch.setattr(builder_settings, "prompt_reserve_output_tokens", 50)
        monkeypatch.setattr(builder_settings, "anchor_best_chunk", True)
        monkeypatch.setattr(builder_settings, "context_max_tokens", 6000)

        from tests.test_prompt_budget import (
            QUERY,
            SYSTEM_PROMPT,
            make_chunk,
        )

        service = PromptBuilderService()
        chunks = [make_chunk(i) for i in range(4)]

        class _Mem:
            content = "M"

        messages = service.build_messages(
            user_input=QUERY,
            chat_history=[],
            chunks=chunks,
            memories=[],
            wiki_evidence=[],
            kms_evidence=[],
            system_prompt_override=SYSTEM_PROMPT,
        )

        user_content = messages[-1]["content"]
        # Tier-8 shed the anchor repeat when everything else was exhausted.
        assert "[BEST MATCH" not in user_content
        # Top primary evidence itself survives.
        assert "[S1]" in user_content
        # Report records the anchor shed.
        assert service.last_budget_report is not None
        assert service.last_budget_report["shed_anchor"] == 1

    def test_anchor_kept_when_budget_generous(self, monkeypatch):
        from app.config import settings as real_settings_module

        builder_settings = real_settings_module
        monkeypatch.setattr(builder_settings, "prompt_budget_enabled", True)
        monkeypatch.setattr(builder_settings, "model_context_tokens", 200000)
        monkeypatch.setattr(builder_settings, "prompt_reserve_output_tokens", 256)
        monkeypatch.setattr(builder_settings, "anchor_best_chunk", True)
        monkeypatch.setattr(builder_settings, "context_max_tokens", 6000)

        from tests.test_prompt_budget import QUERY, SYSTEM_PROMPT, make_chunk

        service = PromptBuilderService()
        chunks = [make_chunk(i) for i in range(2)]
        messages = service.build_messages(
            user_input=QUERY,
            chat_history=[],
            chunks=chunks,
            memories=[],
            wiki_evidence=[],
            kms_evidence=[],
            system_prompt_override=SYSTEM_PROMPT,
        )
        user_content = messages[-1]["content"]
        assert "[BEST MATCH" in user_content
        assert service.last_budget_report["shed_anchor"] == 0


class TestPRR010NaNLogitGuard:
    def test_nan_logit_maps_to_zero(self):
        assert _safe_sigmoid(float("nan")) == 0.0

    def test_no_nan_ever_escapes(self):
        for raw in (float("nan"), float("inf"), float("-inf"), 709.0, -709.0, 0.0):
            score = _safe_sigmoid(raw)
            assert not math.isnan(score), raw
            assert not math.isinf(score), raw
