"""Acceptance checks (C16, C6/C7) — linear-time guards in query_transformer.

- C6 (DISCRIMINATING): ``_is_exact_or_document_query`` must run in linear
  time on a 200,000-char hyphen-run input (the filename regex's greedy
  class + backtracking is quadratic at the pre-fix base).
  NOTE: the base leg of this check is SLOW BY DESIGN — at the pre-fix
  base the call itself takes roughly 160 seconds before the duration
  assert fails (RED). Do not add a timeout that would mask the slowness.
- C7 (PRESERVING): ``_normalize_sub_query`` must run in linear time on a
  trailing punctuation run (the str.rstrip shape), while still stripping
  the trailing sentence-punctuation set ".?!,;:". GREEN at the base
  (the #919 str.rstrip fix).
"""

import time

from app.services.query_transformer import (
    _is_exact_or_document_query,
    _normalize_sub_query,
)


def test_exact_or_document_query_is_linear_on_hyphen_run():
    """C6: 200,000-char hyphen run must classify in under a second.

    Base leg is slow by design (~160 s): the assert below then fails
    with the measured duration, which is the RED signal for this check.
    """
    payload = "a-" * 100_000  # 200,000 chars, no dots, no quotes
    start = time.perf_counter()
    _is_exact_or_document_query(payload)
    duration = time.perf_counter() - start
    assert duration < 1.0, (
        "_is_exact_or_document_query took %.1fs on a 200,000-char hyphen "
        "run; the filename regex must be linear-time, not quadratic "
        "(greedy [\\w\\-]+ followed by a literal dot backtracks over the "
        "whole run at every start position)" % duration
    )


def test_normalize_sub_query_is_linear_on_punctuation_run():
    """C7: a 20,000-char trailing punctuation run normalizes in under a
    second, with the trailing-punctuation semantics preserved."""
    payload = "word " * 8000 + "." * 20000
    start = time.perf_counter()
    result = _normalize_sub_query(payload)
    duration = time.perf_counter() - start
    assert duration < 1.0, (
        "_normalize_sub_query took %.1fs on a 60,000-char payload with a "
        "20,000-char trailing punctuation run; normalization must stay "
        "linear (str.rstrip on the punctuation set, not a regex "
        "substitution)" % duration
    )
    assert result[-1] not in ".?!,;:", (
        "semantics preserved: trailing sentence punctuation from the set "
        "'.?!,;:' must still be stripped"
    )
    assert result.startswith("word"), (
        "semantics preserved: the leading content must survive normalization"
    )
