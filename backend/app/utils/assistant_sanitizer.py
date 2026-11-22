"""Centralized sanitizer for user-visible assistant content.

This module is the single source of truth for stripping model thinking /
reasoning traces from any assistant text that may be shown to users or
persisted in chat history. It handles four distinct patterns:

1. ``_lhs ... _rhs`` thinking blocks (legacy Qwen tag style).
2. ``Thinking Process: ... </think>`` prefix style (qwen3.5-122b).
3. Standard ``<think>...</think>`` thinking blocks (gpt-oss-120b and others).
4. ``Thinking Process:`` followed by a ``</think>`` close even without an
   opening tag (defensive — fragmented streams may emit just the close).
5. ``<answer>`` / ``</answer>`` wrapper TAGS (content preserved — the inner
   text is the answer; issue #1153 AC1).

All functions are idempotent: calling them twice on already-sanitized text
returns the same result.

The streaming code path additionally needs incremental, chunk-aware
sanitization which lives in ``LLMClient.chat_completion_stream`` and uses
:func:`is_thinking_open_marker`/:func:`is_thinking_close_marker` for guidance.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

# Module-level constants — each is a literal regex pattern.
# Use ``re.DOTALL`` so ``.`` spans newlines.
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_LHS_RHS_RE = re.compile(r"_lhs.*?_rhs", re.DOTALL)
# Match unterminated <think> from a stripped close tag (e.g. partial chunk leak)
_UNTERMINATED_THINK_TAIL_RE = re.compile(r"<think\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
# Thinking Process: ... </think>  (qwen3.5-122b style)
_THINKING_PROCESS_BLOCK_RE = re.compile(
    r"Thinking Process:.*?</think>\s*", re.DOTALL | re.IGNORECASE
)
# Defensive: orphan "Thinking Process:" prefix at start with no close tag —
# strip the leading line up to a blank line if present.
_THINKING_PROCESS_PREFIX_RE = re.compile(
    r"^Thinking Process:[^\n]*\n", re.IGNORECASE
)
# Answer wrapper TAGS (issue #1153 AC1): some models wrap the final answer in
# ``<answer>...</answer>`` elements. Unlike thinking blocks (removed with their
# content), only the TAGS are stripped — the inner text IS the answer. Matches
# openers with attributes and any casing; the pair need not be balanced on this
# pass (a lone closer is equally stripped), and the pass is idempotent.
_ANSWER_WRAPPER_OPEN_RE = re.compile(r"<answer\b[^>]*>", re.IGNORECASE)
_ANSWER_WRAPPER_CLOSE_RE = re.compile(r"</answer\s*>", re.IGNORECASE)

_OPEN_FENCE_RE = re.compile(r"(`{3,}|~{3,})")


def _strip_answer_tags(text: str) -> str:
    """Remove ``<answer>``/``</answer>`` wrapper tags from a run of text.

    Applied to the WHOLE run (not line-by-line) so a wrapper tag that spans
    lines is still removed — the pre-fence-awareness behavior.
    """
    return _ANSWER_WRAPPER_CLOSE_RE.sub(
        "", _ANSWER_WRAPPER_OPEN_RE.sub("", text)
    )


def _find_code_fence_spans(text: str) -> List[Tuple[int, int]]:
    """Return the ``(start, end)`` char offsets of fenced code blocks.

    Honours CommonMark so the protection is correct, not just present:
    - an opening fence is 3+ backticks or tildes (up to 3 spaces indented); a
      backtick fence's info string may not itself contain a backtick, so
      `` ```a`b `` is NOT a fence and does not shield the rest of the text;
    - a closing fence is the same marker character, at least as long, with only
      trailing whitespace — so a longer closer (```` vs ```) terminates it;
    - an unterminated fence runs to the end of the text.
    CRLF line endings are handled (the ``\\r`` is whitespace for detection).
    """
    spans: List[Tuple[int, int]] = []
    pos = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_start = 0
    for line in text.split("\n"):
        line_start = pos
        trimmed = line.strip()
        if not in_fence:
            open_match = _OPEN_FENCE_RE.match(trimmed)
            if open_match:
                marker = open_match.group(1)
                marker_char = marker[0]
                info = trimmed[len(marker):]
                # A backtick info string may not contain a backtick.
                if marker_char == "`" and "`" in info:
                    pass  # not a valid opener
                else:
                    in_fence = True
                    fence_char = marker_char
                    fence_len = len(marker)
                    fence_start = line_start
        elif trimmed and all(c == fence_char for c in trimmed) and len(
            trimmed
        ) >= fence_len:
            # Closing fence: include through the end of this line (the "\n"
            # separator is added by the next iteration's accounting).
            spans.append((fence_start, line_start + len(line)))
            in_fence = False
        pos = line_start + len(line) + 1
    if in_fence:
        spans.append((fence_start, len(text)))
    return spans


def _strip_answer_wrappers_outside_code_fences(text: str) -> str:
    """Strip ``<answer>``/``</answer>`` wrapper tags only in non-fenced text.

    A literal ``<answer>`` inside a fenced code block (an XML/HTML example, a
    documentation snippet) is real content and must survive — both at runtime
    and in the startup re-sanitize migration, which would otherwise
    permanently rewrite historical messages. Only the wrapper tags outside
    fences are removed (issue #1153 AC1).
    """
    spans = _find_code_fence_spans(text)
    if not spans:
        return _strip_answer_tags(text)
    out: List[str] = []
    last = 0
    for start, end in spans:
        out.append(_strip_answer_tags(text[last:start]))
        out.append(text[start:end])  # fenced block: verbatim
        last = end
    out.append(_strip_answer_tags(text[last:]))
    return "".join(out)


def sanitize_assistant_content(content: str) -> str:
    """Return ``content`` with all known thinking-content patterns stripped.

    Idempotent and safe to call on partial or fully-sanitized strings.

    Strips:
    - ``<think>...</think>`` blocks (any case, any attributes)
    - ``_lhs ... _rhs`` blocks
    - ``Thinking Process: ... </think>`` blocks
    - Unterminated trailing ``<think>...`` tail (defensive)

    Args:
        content: Raw assistant text that may contain thinking traces.

    Returns:
        The sanitized text. Whitespace is trimmed at both ends.
    """
    if not content:
        return ""
    out = content

    # Order matters: handle complete blocks first, then unterminated trailers.
    # 1. Standard <think>...</think>
    out = _THINK_BLOCK_RE.sub("", out)
    # 2. _lhs ... _rhs
    out = _LHS_RHS_RE.sub("", out)
    # 3. "Thinking Process: ... </think>"
    out = _THINKING_PROCESS_BLOCK_RE.sub("", out)
    # 4. Trailing unterminated <think>... — strip everything from the open tag
    #    onward. This guards against persistence of a half-streamed thinking
    #    block when sanitization is applied after-the-fact.
    out = _UNTERMINATED_THINK_TAIL_RE.sub("", out)
    # 5. Leading "Thinking Process:" line without a close — defensive last pass
    out = _THINKING_PROCESS_PREFIX_RE.sub("", out)
    # 6. Answer wrapper TAGS — strip the delimiters, keep the inner answer
    #    (issue #1153 AC1: the tags reached persisted/returned content on a
    #    deployed build). Fenced-code-aware so a literal ``<answer>`` inside a
    #    code example is not corrupted (review F-07); idempotent.
    out = _strip_answer_wrappers_outside_code_fences(out)

    return out.strip()


def sanitize_chat_messages_content(content: str) -> str:
    """Sanitize a chat_messages.content value before persistence.

    Equivalent to :func:`sanitize_assistant_content`. Kept as a separate
    name so callers can grep for "what runs at the persistence boundary"
    independent of LLM-time stripping.
    """
    return sanitize_assistant_content(content)


def cleanup_existing_chat_messages_rows(rows: Iterable[tuple]) -> List[tuple]:
    """Idempotent cleanup helper for already-persisted rows.

    Accepts an iterable of ``(id, content)`` tuples and returns a list of
    ``(id, sanitized_content)`` tuples for rows whose content actually
    changes after sanitization. Rows whose content is unchanged are
    omitted so callers can do a no-op fast path.
    """
    out: List[tuple] = []
    for row in rows:
        if not row or len(row) < 2:
            continue
        row_id, content = row[0], row[1]
        if not isinstance(content, str):
            continue
        cleaned = sanitize_assistant_content(content)
        if cleaned != content:
            out.append((row_id, cleaned))
    return out


# Constants exposed for streaming sanitizer use ----------------------------------

# Substrings that, when present in a stream buffer, indicate the start of a
# thinking block. The streaming sanitizer uses these as discrete tokens.
THINKING_OPEN_MARKERS = ("<think>", "_lhs", "Thinking Process:")
# Substrings indicating end of a thinking block.
THINKING_CLOSE_MARKERS = ("</think>", "_rhs")


def is_thinking_open_marker(buf: str) -> bool:
    """Return True if ``buf`` contains an unambiguous thinking-open marker."""
    return any(m in buf for m in THINKING_OPEN_MARKERS)


def is_thinking_close_marker(buf: str) -> bool:
    """Return True if ``buf`` contains an unambiguous thinking-close marker."""
    return any(m in buf for m in THINKING_CLOSE_MARKERS)
