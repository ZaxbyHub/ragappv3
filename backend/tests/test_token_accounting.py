"""Tests for token accounting (issue #511 FULL-ENH-01, AC8).

Covers ``app.services.token_accounting.count_tokens``:
- positive int counts for English prose, CJK-heavy text, code, and a table
- the CJK sample (60 CJK chars) counts >= 40 tokens — the legacy len/3.5
  heuristic (~17) badly undercounts CJK; a conservative estimator must not
- determinism (same input -> same count)
- the conservative script-aware fallback works with the tiktoken import
  blocked (sys.modules sentinel) and never undercounts CJK vs len/3.5
- when tiktoken is usable in the ambient env, the English count stays within
  3x of tiktoken's own count (informational sanity bound)
"""

import importlib
import math
import sys
from types import SimpleNamespace

import pytest

from app.services import token_accounting
from app.services.token_accounting import count_tokens, fallback_count_tokens

ENGLISH = (
    "The retrieval pipeline indexes documents into a vector store, fuses "
    "dense and keyword search, reranks the strongest passages, and assembles "
    "the final prompt for the language model to answer."
)
# Exactly 60 CJK characters (CJK Unified Ideographs).
CJK_BASE = "知识管理系统通过向量检索与关键词搜索融合排序来回答用户提出的问题"
CJK = (CJK_BASE * 2)[:60]
CODE = (
    "def tokenize(text: str) -> list[int]:\n"
    "    tokens = []\n"
    "    for line in text.splitlines():\n"
    "        tokens.append(len(line.strip()))\n"
    "    return tokens\n"
)
TABLE = (
    "| model | ctx | out |\n"
    "|-------|-------|------|\n"
    "| a | 8192 | 512 |\n"
    "| b | 32768 | 1024 |\n"
)


class TestCountTokensSamples:
    """Positive-int counts and determinism across content shapes."""

    @pytest.mark.parametrize(
        "name,sample",
        [("english", ENGLISH), ("cjk", CJK), ("code", CODE), ("table", TABLE)],
    )
    def test_positive_int(self, name, sample):
        count = count_tokens(sample)
        assert isinstance(count, int) and not isinstance(count, bool)
        assert count > 0

    @pytest.mark.parametrize(
        "name,sample",
        [("english", ENGLISH), ("cjk", CJK), ("code", CODE), ("table", TABLE)],
    )
    def test_deterministic(self, name, sample):
        assert count_tokens(sample) == count_tokens(sample)

    def test_empty_string_is_zero(self):
        assert count_tokens("") == 0

    def test_cjk_not_undercounted_vs_len_3_5(self):
        """60 CJK chars must count >= 40 tokens (len/3.5 would say ~17)."""
        assert len(CJK) == 60
        assert count_tokens(CJK) >= 40

    def test_tiktoken_ratio_when_usable(self):
        """English count within 3x of tiktoken's own count (when importable)."""
        try:
            import tiktoken
        except ImportError:
            pytest.skip("tiktoken not installed in this environment")
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            reference = len(enc.encode(ENGLISH))
        except Exception as exc:  # noqa: BLE001 — offline cache miss etc.
            pytest.skip(f"tiktoken encoding unavailable: {exc}")
        ratio = count_tokens(ENGLISH) / max(1, reference)
        assert 1 / 3 <= ratio <= 3


class TestFallbackCountTokens:
    """The pure-stdlib conservative estimator."""

    def test_empty_is_zero(self):
        assert fallback_count_tokens("") == 0

    def test_cjk_char_is_one_token_each(self):
        assert fallback_count_tokens(CJK) == 60

    def test_ascii_grouped_in_threes(self):
        assert fallback_count_tokens("abc") == 1
        assert fallback_count_tokens("abcd") == 2  # ceil(4/3)
        assert fallback_count_tokens("abcdef") == 2

    def test_mixed_cjk_and_ascii(self):
        # 2 CJK chars (1 each) + 3 ascii chars (ceil(3/3) = 1) = 3
        assert fallback_count_tokens("知识abc") == 3

    def test_never_below_len_3_5_for_cjk(self):
        """The fallback must never undercount CJK vs the legacy heuristic."""
        assert fallback_count_tokens(CJK) >= math.ceil(len(CJK) / 3.5)

    def test_fullwidth_and_kana_ranges_counted_as_cjk(self):
        # Fullwidth forms (U+FF00-FFEF) and a hiragana char (U+3042, inside
        # U+3000-9FFF) each count as one full token.
        assert fallback_count_tokens("ａｂ") == 2
        assert fallback_count_tokens("あ") == 1

    def test_deterministic(self):
        assert fallback_count_tokens(ENGLISH) == fallback_count_tokens(ENGLISH)


class TestTiktokenBlockedFallback:
    """With the tiktoken import blocked, count_tokens still works."""

    @pytest.fixture
    def blocked_module(self):
        """Import a fresh token_accounting with ``import tiktoken`` failing.

        Setting ``sys.modules["tiktoken"] = None`` makes ``import tiktoken``
        raise ImportError (the import-system sentinel for a failed import).
        """
        saved = sys.modules.get("tiktoken", "…absent…")
        saved_module = sys.modules.get(token_accounting.__name__)
        sys.modules["tiktoken"] = None
        try:
            sys.modules.pop(token_accounting.__name__, None)
            fresh = importlib.import_module(token_accounting.__name__)
            yield fresh
        finally:
            sys.modules.pop(token_accounting.__name__, None)
            if saved_module is not None:
                sys.modules[token_accounting.__name__] = saved_module
            if saved == "…absent…":
                sys.modules.pop("tiktoken", None)
            else:
                sys.modules["tiktoken"] = saved

    def test_blocked_import_still_counts(self, blocked_module):
        count = blocked_module.count_tokens(ENGLISH)
        assert isinstance(count, int) and count > 0

    def test_blocked_import_cjk_not_undercounted(self, blocked_module):
        assert blocked_module.count_tokens(CJK) >= 40

    def test_blocked_import_matches_fallback(self, blocked_module):
        assert blocked_module.count_tokens(ENGLISH) == fallback_count_tokens(
            ENGLISH
        )

    def test_get_encoding_failure_falls_back(self, monkeypatch):
        """A tiktoken that imports but fails at get_encoding falls back."""
        calls = {"n": 0}

        def broken_get_encoding(name):
            calls["n"] += 1
            raise RuntimeError("no BPE cache offline")

        monkeypatch.setattr(
            token_accounting,
            "_tiktoken",
            SimpleNamespace(get_encoding=broken_get_encoding),
        )
        # Reset any cached encoding from earlier tests in this module run.
        # monkeypatch restores the originals at teardown.
        monkeypatch.setattr(token_accounting, "_encoding", None)
        monkeypatch.setattr(token_accounting, "_encoding_failed", False)

        count = count_tokens(ENGLISH)
        assert count == fallback_count_tokens(ENGLISH)
        assert calls["n"] == 1  # lazy init attempted exactly once
        # The failure is sticky: the next call does not retry get_encoding.
        assert count_tokens(ENGLISH) == fallback_count_tokens(ENGLISH)
        assert calls["n"] == 1
