"""Token accounting for model-aware prompt budgets (issue #511 FULL-ENH-01).

``count_tokens(text)`` is the single estimator used by the prompt-budget
pass in ``prompt_builder``. It prefers a real BPE tokenizer:

- When the optional ``tiktoken`` package imports AND its encoding file is
  available, ``cl100k_base`` is used (lazily, once per process; a failed
  ``get_encoding`` — e.g. an offline first run with no BPE cache — is
  sticky-remembered and never retried).
- Otherwise a conservative script-aware fallback estimates from characters.

Why the fallback is conservative (never an undercount for CJK):
the legacy inline heuristic ``len(text) / 3.5`` assumes ~3.5 characters per
token, which is roughly right for English BPE but catastrophically wrong for
CJK scripts, where one character is typically ONE OR MORE tokens (~1-1.7 for
cl100k-class tokenizers on common Han text). ``len/3.5`` therefore reports
~17 tokens for a 60-character Chinese sentence that really costs ~60+.
The fallback counts every CJK-range character as a full token and groups
remaining (mostly ASCII) characters at 3 per token — an upper-bound-ish
estimate that errs toward over-budgeting prompts instead of silently
truncating the model's context window mid-request.

Deterministic and pure-stdlib on the fallback path.
"""

import logging
import math
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:  # optional dependency: never required, never hard-failed
    import tiktoken as _tiktoken
except ImportError:  # pragma: no cover - depends on environment
    _tiktoken = None

_ENCODING_NAME = "cl100k_base"

# Module-level lazily-initialized encoding (None until first successful use).
_encoding: Optional[Any] = None
# Sticky failure flag: once get_encoding errors (e.g. offline, no BPE cache),
# stop retrying and always use the fallback.
_encoding_failed = False

# Ranges where one character is conservatively a full token: CJK Unified
# Ideographs + surrounding CJK punctuation/symbols/kana (U+3000-U+9FFF),
# CJK Compatibility Ideographs (U+F900-U+FAFF), and fullwidth forms /
# halfwidth-fullwidth block (U+FF00-U+FFEF). Defined in ONE place.
_CJK_RANGES = (
    (0x3000, 0x9FFF),  # CJK symbols/punctuation, kana, common Han
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # fullwidth / halfwidth forms
)

# Non-CJK characters per token in the fallback estimate (ASCII-ish scripts
# average >= 3 chars/token under BPE tokenizers).
_NON_CJK_CHARS_PER_TOKEN = 3


def _is_cjk_char(char: str) -> bool:
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def fallback_count_tokens(text: str) -> int:
    """Conservative script-aware character estimate (pure stdlib).

    CJK-range characters count 1 token each; every other character counts
    1/3 token (grouped, ceil). Non-empty text always counts at least 1.
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for char in text:
        if _is_cjk_char(char):
            cjk += 1
        else:
            other += 1
    return max(cjk + math.ceil(other / _NON_CJK_CHARS_PER_TOKEN), 1)


def _get_encoding() -> Optional[Any]:
    """Return the lazily-initialized tiktoken encoding, or None when the
    tokenizer is unavailable (not installed or get_encoding failed)."""
    global _encoding, _encoding_failed
    if _encoding is not None:
        return _encoding
    if _encoding_failed or _tiktoken is None:
        return None
    try:
        _encoding = _tiktoken.get_encoding(_ENCODING_NAME)
    except Exception:
        # Offline without the BPE cache, corrupted install, etc. — fall
        # back permanently rather than paying the failure on every call.
        _encoding_failed = True
        return None
    return _encoding


def count_tokens(text: str) -> int:
    """Estimate the token count of ``text``.

    Uses tiktoken (cl100k_base) when available; otherwise the conservative
    script-aware fallback. Deterministic for a given environment. Empty text
    is 0; non-empty text is always >= 1.
    """
    if not text:
        return 0
    encoding = _get_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text))
        except Exception as enc_error:
            # Pathological input for the tokenizer — fall through to the
            # conservative estimate rather than failing the caller.
            logger.debug("tiktoken encode failed (%s); using conservative fallback", enc_error)
    return fallback_count_tokens(text)
